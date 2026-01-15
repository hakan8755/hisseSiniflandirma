import time
import csv
import undetected_chromedriver as uc
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException
from bs4 import BeautifulSoup


def open_driver():
    options = uc.ChromeOptions()
    options.page_load_strategy = "eager"
    options.add_argument("--start-maximized")
    # İstersen biraz daha insani görünmek için user-agent ekleyebilirsin:
    # options.add_argument(
    #     "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    #     "AppleWebKit/537.36 (KHTML, like Gecko) "
    #     "Chrome/120.0.0.0 Safari/537.36"
    # )
    driver = uc.Chrome(options=options)
    driver.set_page_load_timeout(30)
    return driver


def scrape_investing_comments(base_url, start_page=1, end_page=10):
    """
    Investing Tesla yorum sayfalarından (#comments_new içindeki)
    tüm yorum metinlerini çeker.
      base_url = "https://tr.investing.com/equities/tesla-motors-commentary"
      start_page=1, end_page=10  -> 1–10. sayfa
    """
    all_comments = []

    for page in range(start_page, end_page + 1):
        if page == 1:
            url = base_url
        else:
            url = f"{base_url}/{page}"

        print(f"\n{'=' * 40}\n📄 {page}. SAYFA YÜKLENİYOR: {url}\n{'=' * 40}")

        # Her sayfa için driver'ı baştan aç
        try:
            driver = open_driver()
        except Exception as e:
            print(f"🚫 WebDriver başlatılırken hata oluştu: {e}")
            break

        try:
            try:
                driver.get(url)
            except TimeoutException:
                print("⚠️ Sayfa 30 saniye içinde yüklenemedi (Timeout). Atlaniyor...")
                driver.quit()
                continue

            # Yorum container'ının geldiğinden emin ol
            WebDriverWait(driver, 15).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "#comments_new"))
            )
            time.sleep(1.5)  # biraz otursun

            soup = BeautifulSoup(driver.page_source, "html.parser")

            # Asıl yorum text'i: #comments_new içindeki div.break-words.leading-5
            comment_divs = soup.select("#comments_new div.break-words.leading-5")

            page_comments = []
            for idx, cdiv in enumerate(comment_divs, start=1):
                text = cdiv.get_text(" ", strip=True)
                if not text:
                    continue

                page_comments.append(
                    {
                        "page": page,
                        "index_in_page": idx,
                        "comment": text,
                    }
                )

            print(f"✅ {page}. sayfadan {len(page_comments)} yorum çekildi.")
            all_comments.extend(page_comments)

        except Exception as e:
            print(f"⚠️ {page}. sayfa işlenirken hata: {e}")
        finally:
            # Bu sayfanın driver'ını kapat
            try:
                driver.quit()
            except:
                pass

        # Bot gibi görünmemek için ufak bekleme
        time.sleep(2)

    return all_comments


# ===================== ÇALIŞTIRMA ===================== #
if __name__ == "__main__":
    BASE_URL = "https://tr.investing.com/equities/tesla-motors-commentary"

    # İlk deneme için 1–10. sayfalar
    comments = scrape_investing_comments(BASE_URL, start_page=1, end_page=10)

    if comments:
        file_name = "tesla_yorumlari_sayfa1-10.csv"
        fieldnames = ["page", "index_in_page", "comment"]

        print(f"\n💾 Toplam {len(comments)} yorum '{file_name}' dosyasına kaydediliyor...")
        with open(file_name, "w", newline="", encoding="utf-8-sig") as csvfile:
            writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(comments)
        print(f"✅ İşlem tamamlandı! Dosya: {file_name}")
    else:
        print("❌ Hiç yorum çekilemedi. Selector'ları veya siteyi tekrar kontrol etmek gerekebilir.")
