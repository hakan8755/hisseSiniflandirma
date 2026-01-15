import csv
import json
import os
import random
import time
import hashlib
import re
from dataclasses import dataclass

import undetected_chromedriver as uc
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import (
    TimeoutException,
    WebDriverException,
    SessionNotCreatedException,
)

# ========= AYARLAR =========
CHROME_PROFILE_DIR = r"C:\investing_uc_profile"   # <-- BUNU AYARLA (Windows)
CHROME_PROFILE_NAME = "Default"                   # genelde Default


@dataclass
class ScrapeConfig:
    base_url: str
    out_csv: str = "yorumlar.csv"
    progress_file: str = "progress.json"
    max_pages: int = 10_000

    # HIZ / STABİLİTE
    wait_sec: int = 4                 # 8 -> 4 (hız)
    page_load_timeout: int = 25       # 30 -> 25 (hız)

    # İNSANİ BEKLEME (agresif hız)
    min_sleep: float = 0.15           # 1.2 -> 0.15
    max_sleep: float = 0.55           # 3.0 -> 0.55

    # Chrome davranışı
    page_load_strategy: str = "eager"

    # Driver restart (çok sık restart aşırı yavaşlatır)
    restart_every_pages: int = 80     # 5 -> 80

    # Uzun mola (daha seyrek + daha kısa)
    long_break_every_pages: int = 150 # 20 -> 150
    long_break_min_sec: int = 8       # 30 -> 8
    long_break_max_sec: int = 18      # 60 -> 18

    # ✅ URL mode
    url_mode: str = "auto"  # "path" | "query" | "auto"

    # ✅ Lazy-load scroll (daha az)
    scroll_rounds: int = 2            # 4 -> 2
    scroll_step_px: int = 650         # 750 -> 650
    scroll_pause_min: float = 0.12
    scroll_pause_max: float = 0.28

    # ✅ Driver açılış stabilitesi
    driver_open_retries: int = 3
    driver_retry_sleep_min: float = 1.2
    driver_retry_sleep_max: float = 2.2

    # ✅ Profil kilitlenirse otomatik fallback profile
    fallback_profile_dir: str = r"C:\investing_uc_profile_fallback"

    # ✅ Hız: kaynak bloklama
    block_images: bool = True
    block_fonts: bool = True
    block_css: bool = True           # sorun olursa False yap
    block_media: bool = True


CSV_FIELDS = [
    "page",
    "index_in_page",
    "datetime",
    "username",
    "like",
    "dislike",
    "comment_id",
    "comment",
    "hash",
    "source_url",
]


def safe_sleep(cfg: ScrapeConfig, extra: float = 0.0):
    time.sleep(random.uniform(cfg.min_sleep, cfg.max_sleep) + extra)


def comment_hash(text: str) -> str:
    return hashlib.sha1(text.strip().encode("utf-8")).hexdigest()


def load_progress(cfg: ScrapeConfig):
    if os.path.exists(cfg.progress_file):
        with open(cfg.progress_file, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"last_page": 0, "seen_hashes": []}


def save_progress(cfg: ScrapeConfig, last_page: int, seen_hashes: set):
    data = {"last_page": last_page, "seen_hashes": list(seen_hashes)}
    with open(cfg.progress_file, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def ensure_csv_header(cfg: ScrapeConfig):
    if not os.path.exists(cfg.out_csv):
        with open(cfg.out_csv, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
            w.writeheader()


def append_rows(cfg: ScrapeConfig, rows):
    if not rows:
        return
    with open(cfg.out_csv, "a", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        w.writerows(rows)


# ========= ✅ Windows process temizleme =========
def kill_leftover_chrome_processes():
    try:
        os.system("taskkill /F /IM chromedriver.exe >nul 2>&1")
        os.system("taskkill /F /IM chrome.exe >nul 2>&1")
    except Exception:
        pass


def ensure_profile_dir(path: str):
    try:
        os.makedirs(path, exist_ok=True)
    except Exception:
        pass


def apply_speed_prefs(options: uc.ChromeOptions, cfg: ScrapeConfig):
    """
    Selenium tarafı prefs: görsel/bildirim vs.
    CSS'i prefs'ten kapatmak bazen layout bozabilir; asıl bloklama CDP'de.
    """
    prefs = {
        "profile.managed_default_content_settings.images": 2 if cfg.block_images else 1,
        "profile.default_content_setting_values.notifications": 2,
        "profile.managed_default_content_settings.cookies": 1,
        "profile.managed_default_content_settings.javascript": 1,
        "profile.managed_default_content_settings.popups": 2,
        "profile.managed_default_content_settings.geolocation": 2,
        "profile.managed_default_content_settings.media_stream": 2,
    }
    options.add_experimental_option("prefs", prefs)


def apply_speed_cdp(driver, cfg: ScrapeConfig):
    """
    CDP ile network bloklama: en etkili hızlandırma.
    """
    try:
        driver.execute_cdp_cmd("Network.enable", {})
        blocked = []

        if cfg.block_images:
            blocked += ["*.png", "*.jpg", "*.jpeg", "*.gif", "*.webp", "*.svg", "*.ico"]
        if cfg.block_fonts:
            blocked += ["*.woff", "*.woff2", "*.ttf", "*.otf"]
        if cfg.block_css:
            blocked += ["*.css"]
        if cfg.block_media:
            blocked += ["*.mp4", "*.webm", "*.m3u8"]

        # reklam/izleme
        blocked += [
            "*doubleclick*", "*googlesyndication*", "*google-analytics*",
            "*facebook*", "*hotjar*", "*optimizely*",
        ]

        driver.execute_cdp_cmd("Network.setBlockedURLs", {"urls": blocked})
    except Exception:
        pass


def open_driver(cfg: ScrapeConfig):
    """
    ✅ 'cannot connect to chrome' hatasına karşı:
      - process kill
      - retry
      - fallback profile
      + hız: bloklama/prefs
    """
    ensure_profile_dir(CHROME_PROFILE_DIR)
    ensure_profile_dir(cfg.fallback_profile_dir)

    last_err = None

    for attempt in range(1, cfg.driver_open_retries + 1):
        try:
            kill_leftover_chrome_processes()
            time.sleep(0.4)

            options = uc.ChromeOptions()
            options.page_load_strategy = cfg.page_load_strategy
            options.add_argument("--start-maximized")

            # stabilite flagleri
            options.add_argument("--no-sandbox")
            options.add_argument("--disable-dev-shm-usage")
            options.add_argument("--disable-gpu")
            options.add_argument("--disable-extensions")
            options.add_argument("--disable-popup-blocking")

            apply_speed_prefs(options, cfg)

            options.add_argument(fr"--user-data-dir={CHROME_PROFILE_DIR}")
            options.add_argument(fr"--profile-directory={CHROME_PROFILE_NAME}")

            driver = uc.Chrome(options=options, use_subprocess=True)
            driver.set_page_load_timeout(cfg.page_load_timeout)

            apply_speed_cdp(driver, cfg)
            return driver

        except SessionNotCreatedException as e:
            last_err = e

            # fallback profile
            try:
                kill_leftover_chrome_processes()
                time.sleep(0.6)

                options = uc.ChromeOptions()
                options.page_load_strategy = cfg.page_load_strategy
                options.add_argument("--start-maximized")

                options.add_argument("--no-sandbox")
                options.add_argument("--disable-dev-shm-usage")
                options.add_argument("--disable-gpu")
                options.add_argument("--disable-extensions")
                options.add_argument("--disable-popup-blocking")

                apply_speed_prefs(options, cfg)

                options.add_argument(fr"--user-data-dir={cfg.fallback_profile_dir}")
                options.add_argument(fr"--profile-directory=Default")

                driver = uc.Chrome(options=options, use_subprocess=True)
                driver.set_page_load_timeout(cfg.page_load_timeout)

                apply_speed_cdp(driver, cfg)
                return driver

            except Exception as e2:
                last_err = e2

            time.sleep(random.uniform(cfg.driver_retry_sleep_min, cfg.driver_retry_sleep_max))

        except Exception as e:
            last_err = e
            time.sleep(random.uniform(cfg.driver_retry_sleep_min, cfg.driver_retry_sleep_max))

    raise RuntimeError(f"open_driver başarısız. Son hata: {last_err}")


def close_cookie_popup_if_any(driver):
    candidates = [
        (By.CSS_SELECTOR, "button#onetrust-accept-btn-handler"),
        (By.CSS_SELECTOR, "button[aria-label='Accept']"),
        (By.XPATH, "//button[contains(., 'Kabul') or contains(., 'Accept')]"),
    ]
    for by, sel in candidates:
        try:
            btn = WebDriverWait(driver, 1).until(EC.element_to_be_clickable((by, sel)))
            btn.click()
            time.sleep(0.15)
            return True
        except Exception:
            pass
    return False


def close_signup_modal_if_any(driver):
    try:
        driver.find_element(By.TAG_NAME, "body").send_keys(Keys.ESCAPE)
        time.sleep(0.05)
    except Exception:
        pass

    candidates = [
        (By.CSS_SELECTOR, "[role='dialog'] [aria-label='Close']"),
        (By.CSS_SELECTOR, "[role='dialog'] [aria-label='Kapat']"),
        (By.XPATH, "//div[@role='dialog']//button"),
        (By.XPATH, "//*[contains(@class,'close') or contains(@class,'Close') or @aria-label='Close' or @aria-label='Kapat']"),
    ]
    for by, sel in candidates:
        try:
            el = WebDriverWait(driver, 0.6).until(EC.element_to_be_clickable((by, sel)))
            el.click()
            time.sleep(0.08)
            return True
        except Exception:
            pass

    try:
        driver.execute_script("""
            const selectors = ["[role='dialog']", ".popup", ".modal", ".overlay", ".backdrop"];
            selectors.forEach(s => document.querySelectorAll(s).forEach(el => el.remove()));
            document.body.style.overflow = 'auto';
        """)
        time.sleep(0.05)
        return True
    except Exception:
        return False


# ========= URL OLUŞTURMA =========
def build_page_url_path(base_url: str, page: int) -> str:
    if page == 1:
        return base_url.rstrip("/")
    return f"{base_url.rstrip('/')}/{page}"


def build_page_url_query(base_url: str, page: int) -> str:
    if page == 1:
        return base_url.rstrip("/")
    joiner = "&" if "?" in base_url else "?"
    return f"{base_url.rstrip('/')}{joiner}page={page}"


def build_page_url(cfg: ScrapeConfig, page: int, mode: str = None) -> str:
    mode = mode or cfg.url_mode
    if mode == "query":
        return build_page_url_query(cfg.base_url, page)
    return build_page_url_path(cfg.base_url, page)


def human_scroll_for_comments(driver, cfg: ScrapeConfig):
    for _ in range(max(1, cfg.scroll_rounds)):
        try:
            driver.execute_script(f"window.scrollBy(0, {cfg.scroll_step_px});")
        except Exception:
            pass
        time.sleep(random.uniform(cfg.scroll_pause_min, cfg.scroll_pause_max))
        close_signup_modal_if_any(driver)


def wait_comments_container(driver, wait_sec: int) -> bool:
    try:
        WebDriverWait(driver, wait_sec).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "#comments_new"))
        )
        return True
    except TimeoutException:
        return False


def _extract_like_dislike_from_card(card):
    like = ""
    dislike = ""

    try:
        elems = card.find_elements(By.XPATH, ".//*[@aria-label]")
        for el in elems:
            al = (el.get_attribute("aria-label") or "").lower()
            if like == "" and ("like" in al or "beğen" in al):
                nums = re.findall(r"\d+", al)
                if nums:
                    like = nums[0]
            if dislike == "" and ("dislike" in al or "beğenme" in al):
                nums = re.findall(r"\d+", al)
                if nums:
                    dislike = nums[0]
    except Exception:
        pass

    if like == "" or dislike == "":
        try:
            btns = card.find_elements(By.TAG_NAME, "button")
            nums = []
            for b in btns:
                t = (b.text or "").strip()
                if t.isdigit():
                    nums.append(t)
                else:
                    m = re.findall(r"\d+", t)
                    if m:
                        nums.extend(m)
            if like == "" and len(nums) >= 1:
                like = nums[0]
            if dislike == "" and len(nums) >= 2:
                dislike = nums[1]
        except Exception:
            pass

    if like == "" or dislike == "":
        try:
            spans = card.find_elements(By.XPATH, ".//span[normalize-space(text())!='']")
            nums = []
            for s in spans:
                t = (s.text or "").strip()
                if t.isdigit():
                    nums.append(t)
            if like == "" and len(nums) >= 1:
                like = nums[0]
            if dislike == "" and len(nums) >= 2:
                dislike = nums[1]
        except Exception:
            pass

    return like, dislike


def extract_comments_from_page(driver, page: int, source_url: str):
    rows = []

    container = WebDriverWait(driver, 3).until(
        EC.presence_of_element_located((By.CSS_SELECTOR, "#comments_new"))
    )

    comment_elems = container.find_elements(By.CSS_SELECTOR, "div.break-words.leading-5")

    for idx, ce in enumerate(comment_elems, start=1):
        try:
            comment_text = (ce.text or "").strip()
            if not comment_text:
                continue

            card = ce
            for _ in range(7):  # 8 -> 7 (azıcık hız)
                card = card.find_element(By.XPATH, "./..")
                has_user = len(card.find_elements(By.CSS_SELECTOR, 'a[href^="/members/"]')) > 0
                has_date = len(card.find_elements(By.CSS_SELECTOR, 'span[data-test="comment-date"]')) > 0
                if has_user and has_date:
                    break

            try:
                username = card.find_element(By.CSS_SELECTOR, 'a[href^="/members/"]').text.strip()
            except Exception:
                username = ""

            try:
                dt = card.find_element(By.CSS_SELECTOR, 'span[data-test="comment-date"]').text.strip()
            except Exception:
                dt = ""

            like, dislike = _extract_like_dislike_from_card(card)

            comment_id = ""
            try:
                outer = card.get_attribute("outerHTML") or ""
                m = re.search(r'data-comment-id="(\d+)"', outer)
                if m:
                    comment_id = m.group(1)
                else:
                    m2 = re.search(r'data-id="(\d+)"', outer)
                    if m2:
                        comment_id = m2.group(1)
            except Exception:
                pass

            if comment_id == "":
                try:
                    href = card.find_element(By.CSS_SELECTOR, 'a[href^="/members/"]').get_attribute("href") or ""
                    comment_id = href.split("/members/")[-1].split("/")[0].strip()
                except Exception:
                    comment_id = ""

            h = comment_hash(comment_text)

            rows.append({
                "page": page,
                "index_in_page": idx,
                "datetime": dt,
                "username": username,
                "like": like,
                "dislike": dislike,
                "comment_id": comment_id,
                "comment": comment_text,
                "hash": h,
                "source_url": source_url,
            })

        except Exception:
            continue

    return rows


def load_page_with_retry(driver, url: str, cfg: ScrapeConfig):
    """
    Hızlı akış:
      - comments_new hızlıca gelirse ekstra scroll YAPMA
      - gelmezse: scroll ile tetikle
      - auto mode: path/query fallback
    """
    last_err = None

    def _load_once(target_url: str):
        driver.get(target_url)
        close_cookie_popup_if_any(driver)
        close_signup_modal_if_any(driver)

        ok = wait_comments_container(driver, cfg.wait_sec)
        if not ok:
            human_scroll_for_comments(driver, cfg)
            ok = wait_comments_container(driver, 2)

        # sadece gerektiğinde bir mini scroll daha
        if ok:
            human_scroll_for_comments(driver, cfg)

        close_signup_modal_if_any(driver)
        return ok

    for attempt in range(1, 3):
        try:
            ok = _load_once(url)
            if ok:
                return True, None

            if cfg.url_mode == "auto":
                if "page=" in url:
                    m = re.search(r"page=(\d+)", url)
                    page_num = int(m.group(1)) if m else 2
                    alt_url = build_page_url(cfg, page_num, mode="path")
                else:
                    m = re.search(r"/(\d+)$", url)
                    page_num = int(m.group(1)) if m else 2
                    alt_url = build_page_url(cfg, page_num, mode="query")

                if alt_url and alt_url != url:
                    ok2 = _load_once(alt_url)
                    if ok2:
                        return True, None

            last_err = TimeoutException(f"comments_new not found for url={url}")
            time.sleep(0.6 * attempt)

        except (TimeoutException, WebDriverException) as e:
            last_err = e
            time.sleep(0.8 * attempt)

    return False, last_err


def scrape_investing_comments_auto(cfg: ScrapeConfig):
    ensure_csv_header(cfg)

    progress = load_progress(cfg)
    last_page_done = int(progress.get("last_page", 0))
    seen_hashes = set(progress.get("seen_hashes", []))

    start_page = max(1, last_page_done + 1)
    print(f"▶️ Kaldığın yer: {last_page_done}. Devam sayfası: {start_page}")

    driver = None

    try:
        for page in range(start_page, cfg.max_pages + 1):
            url = build_page_url(cfg, page, mode="path" if cfg.url_mode == "auto" else cfg.url_mode)
            print(f"\n📄 Sayfa {page} -> {url}")

            if driver is None:
                driver = open_driver(cfg)

            ok, err = load_page_with_retry(driver, url, cfg)
            if not ok:
                try:
                    driver.save_screenshot(f"error_page_{page}.png")
                except Exception:
                    pass
                print(f"🚫 Yüklenemedi, atlıyorum. Hata: {err}")
                save_progress(cfg, page - 1, seen_hashes)
                safe_sleep(cfg, extra=0.5)
                continue

            source_url = driver.current_url

            rows = extract_comments_from_page(driver, page, source_url=source_url)
            if not rows:
                print("ℹ️ Bu sayfada yorum yok. Büyük ihtimalle bitti.")
                save_progress(cfg, page - 1, seen_hashes)
                break

            new_rows = []
            for r in rows:
                if r["hash"] in seen_hashes:
                    continue
                seen_hashes.add(r["hash"])
                new_rows.append(r)

            append_rows(cfg, new_rows)
            save_progress(cfg, page, seen_hashes)

            print(f"✅ Bulunan: {len(rows)} | Yeni yazılan: {len(new_rows)} | Toplam unique: {len(seen_hashes)}")

            if cfg.restart_every_pages > 0 and page % cfg.restart_every_pages == 0:
                try:
                    driver.quit()
                except Exception:
                    pass
                driver = None
                safe_sleep(cfg, extra=0.35)

            if cfg.long_break_every_pages > 0 and page % cfg.long_break_every_pages == 0:
                dur = random.uniform(cfg.long_break_min_sec, cfg.long_break_max_sec)
                print(f"⏸️ Uzun mola (anti-ban): {dur:.1f}s")
                time.sleep(dur)

            safe_sleep(cfg)

    finally:
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass


if __name__ == "__main__":
    cfg = ScrapeConfig(
        base_url="https://www.investing.com/equities/tesla-motors-commentary",
        out_csv="tesla_yorumlari.csv",
        progress_file="tesla.json",
        max_pages=7000,

        # hız ayarları yukarıda default zaten hızlı
        url_mode="auto",

        # driver stability
        driver_open_retries=3,
        driver_retry_sleep_min=1.2,
        driver_retry_sleep_max=2.2,
        fallback_profile_dir=r"C:\investing_uc_profile_fallback",

        # bloklar
        block_images=True,
        block_fonts=True,
        block_css=True,
        block_media=True,
    )
    scrape_investing_comments_auto(cfg)
# YENİ