import undetected_chromedriver as uc
import time


class HLTVParser:
    def __init__(self):
        self.driver = None

    def _create_driver(self):
        print("[DEBUG] Создаю драйвер Chrome (version_main=148)...")
        options = uc.ChromeOptions()
        # options.add_argument("--headless") # ВАЖНО: УБЕРИТЕ HEADLESS
        options.add_argument("--disable-blink-features=AutomationControlled")
        options.add_argument("--no-sandbox")

        # Добавляем user-agent, чтобы выглядеть как обычный браузер
        options.add_argument(
            "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36")

        driver = uc.Chrome(options=options, version_main=148)

        # Скрываем признаки WebDriver через JavaScript
        driver.execute_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        return driver

    def get_page(self, url, retries=3):
        if self.driver is None:
            try:
                self.driver = self._create_driver()
            except Exception as e:
                print(f"[CRITICAL] Не удалось создать драйвер: {e}")
                return None

        for attempt in range(retries):
            try:
                print(f"[DEBUG] Запрос {attempt + 1}/{retries}: {url}")
                self.driver.get(url)
                time.sleep(3 + attempt * 2)  # Увеличиваем паузу с каждой попыткой

                html = self.driver.page_source

                # Проверка на наличие защиты
                if "cf-challenge" in html or "Just a moment" in html:
                    print(f"[WARN] Попытка {attempt + 1}: Обнаружена защита Cloudflare!")
                    # Можно добавить рандомную задержку здесь, чтобы запутать бота
                    continue

                if len(html) < 2000:
                    print(f"[WARN] Попытка {attempt + 1}: HTML подозрительно короткий ({len(html)} символов).")
                    continue

                # Если дошли сюда, значит страница прогрузилась нормально
                return html

            except Exception as e:
                print(f"[ERROR] Ошибка при попытке {attempt + 1}: {e}")
                # Если упал драйвер — сбрасываем его
                try:
                    self.driver.quit()
                except:
                    pass
                self.driver = None
                # Пересоздаем драйвер на следующей итерации
                if attempt < retries - 1:
                    self.driver = self._create_driver()

        print(f"[CRITICAL] Не удалось получить страницу после {retries} попыток.")
        return None


parser = HLTVParser()

def get_html_with_cloudflare_bypass(url):
    print(f"[INFO] Вызов парсера для: {url}")
    html = parser.get_page(url)
    if not html:
        print("[WARN] HTML пустой или None")
        return "", 403
    print(f"[DEBUG] Получен HTML длиной: {len(html)} символов")
    return html, 200