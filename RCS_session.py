from pathlib import Path

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.chrome.service import Service as ChromeService
from selenium.webdriver.firefox.service import Service as FirefoxService
from selenium.webdriver.chrome.options import Options as ChromeOptions
from selenium.webdriver.edge.options import Options as EdgeOptions
from selenium.webdriver.firefox.options import Options as FirefoxOptions
from selenium.webdriver import ActionChains
import os
import time
from selenium.webdriver.edge.service import Service as EdgeService
import chromedriver_autoinstaller
from selenium.webdriver.common.keys import Keys 

_SCRIPT_DIR = Path(__file__).resolve().parent


def _log_ok(msg):
    """ASCII-only console log (avoids cp1252 UnicodeEncodeError on Windows)."""
    print(f"[WaWebSession] OK: {msg}", flush=True)


def _log_err(msg):
    print(f"[WaWebSession] ERR: {msg}", flush=True)

search_xpath='//input[contains(@aria-label,"Search or start")]'
input_text_area = "//div[@aria-placeholder='Type a message']/p"
close_button ="//button[@class='_18eKe']"
status_button_xpath='//button[@aria-label="Status"]'
send_button='//div[@aria-lable="Send"]'
exception_counter =0


class GoogleRCS():
    def __init__(self,browser="chrome",headless=False):
        self.browser=browser
        self.headless=headless
        self.session=False
        if self.browser=="chrome":
            self.driver = self.get_chrome_driver()
        elif self.browser=="edge":
            self.driver = self.get_edge_driver()
        elif self.browser=="firefox":
            self.driver = self.get_firefox_driver()
        else:
            raise Exception("Browser not supported")
                
    def opensession(self):
        self.driver.get("https://messages.google.com/web/conversations")
        WebDriverWait(self.driver, 30).until(
            lambda driver: driver.execute_script("return document.readyState") == "complete"
        )
        _log_ok("Page completely loaded")
        self.wait = WebDriverWait(self.driver,300)
        # Google Messages Web UI varies; match any known "session ready" control
        # ready_xpath = "//span[normalize-space()='Pair with QR code']"
        chat_list_xpath = "//div[contains(@class,'fab-label') and normalize-space()='Start chat']"
        if EC.presence_of_element_located((By.XPATH, chat_list_xpath)):
            _log_ok("Google Messages Web session ready (Start chat visible)")
        else:
            self.wait.until(EC.presence_of_element_located((By.XPATH, chat_list_xpath)))
            _log_ok("Waiting for user to complete QR code login...")
        _log_ok("Google Messages Web session ready (Start chat visible)")
        
    
        _log_ok("User login completed successfully")
        time.sleep(2)  # Optional: wait a moment to ensure session is fully established
        self.session = True
        return self.wait

    def closeSession(self):
        self.driver.close()
        _log_err("Session closed")
        self.session=False
    
    def get_chrome_driver(self):
        drive_path=chromedriver_autoinstaller.install(cwd=True, no_ssl=True,path=os.getcwd())
        chrome_options = ChromeOptions()
        # Basic Chrome driver options
        # chrome_options.add_argument('--disable-application-cache')
        # chrome_options.add_argument("--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

        # # chrome_options.add_argument('--disable-gpu')  # Disable GPU (for headless mode)
        # chrome_options.add_argument('--disable-extensions')
        # chrome_options.add_argument('--no-sandbox')
        # chrome_options.add_argument('--headless')  if self.headless else None # Uncomment to run Chrome in headless mode
        # chrome_options.add_argument('--disable-dev-shm-usage')  # Overcome limited resource issues
        # chrome_options.add_argument('--start-maximized')  # Start Chrome maximized

        #####################
        # Remove arguments that make it obvious this is an automated browser
        chrome_options.add_argument('--disable-blink-features=AutomationControlled')  # Avoid detection as a bot
        chrome_options.add_experimental_option('excludeSwitches', ['enable-automation'])  # Disable 'Chrome is being controlled by automated test software' banner
        chrome_options.add_experimental_option('useAutomationExtension', False)

        # Set user agent to mimic a regular browser
        chrome_options.add_argument("--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

        # Cache and GPU-related arguments: keep for specific needs or remove to mimic original behavior
        # (Leave application cache and GPU options commented unless necessary)
        # chrome_options.add_argument('--disable-application-cache')
        # chrome_options.add_argument('--disable-gpu')

        # Avoid using arguments that are not needed for mimicking a real browser
        # Remove --disable-extensions and --disable-dev-shm-usage unless there's a known limitation
        # chrome_options.add_argument('--disable-extensions')
        # chrome_options.add_argument('--disable-dev-shm-usage')

        # Keep the browser maximized for realism
        chrome_options.add_argument('--start-maximized')

        # Set sandboxing to its original state for normal browser behavior
        # (Remove --no-sandbox unless running in an environment like Docker)
        # chrome_options.add_argument('--no-sandbox')

        # Ensure headless mode is only used when explicitly needed
        if self.headless:
            chrome_options.add_argument('--headless')

        # Optional: Mimic specific viewport or resolution, if required
        chrome_options.add_argument('--window-size=1920,1080')

        # Optional: Enable logging to debug issues (for development purposes)
        chrome_options.add_argument('--enable-logging')
        chrome_options.add_argument('--v=1')
        
        if os.name == "nt":
            _log_ok("Windows OS detected")
            profile_path = str(_SCRIPT_DIR / "chrome_session")
            chrome_options.add_argument(f"user-data-dir={profile_path}")
        else:
            _log_ok("Linux OS detected")
            profile_path = r"/home/dheerain/.config/google-chrome/Default"
            chrome_options.add_argument(f"user-data-dir={profile_path}")  # Set profile path
            executable_path = './chromedriver_linux'
            # Linux-specific options
            chrome_options.add_argument('--disable-notifications')  # Disable notifications
            chrome_options.add_argument('--disable-popup-blocking')  # Allow popups if needed

        # chrome_driver_path = os.path.join(os.getcwd(), "chromedriver")  # Assuming 'chromedriver' is in the current directory
        service = ChromeService(drive_path)
        dr = webdriver.Chrome(service=service,options=chrome_options)
        return dr
    
    def get_edge_driver(self):
        edge_driver_path = os.path.join(os.getcwd(), "msedgedriver.exe")  # Assuming 'msedgedriver' is in the current directory
        # Use local EdgeDriver from the current directory
        edge_options = EdgeOptions()
        if self.headless:
            _log_ok("Headless mode enabled")
            edge_options.add_argument('--headless')  # Enable headless mode
            edge_options.add_argument('--disable-gpu')  # Disable GPU acceleration (useful in headless mode)
            edge_options.add_argument('--window-size=1920,1080')  # Set window size for headless mode

        if os.name == "nt":  # Windows OS
            _log_ok("Windows OS detected")
            profile_path = str(_SCRIPT_DIR / "edge_session")
            edge_options.add_argument(f"user-data-dir={profile_path}")
            # edge_options.add_argument("--profile-directory=Default")  # Use the appropriate profile
            # edge_driver_path = 'msedgedriver.exe'  # Ensure the EdgeDriver is in the same directory or specify the full path
        else:  # Linux OS
            _log_ok("Linux OS detected")
            profile_path = r"/home/dheerain/.config/microsoft-edge/Default"
            edge_options.add_argument(f"user-data-dir={profile_path}")  # Set profile path
            edge_driver_path = './msedgedriver_linux'  # Ensure you have the correct EdgeDriver binary for Linux

        # Linux-specific options
        # edge_options.add_argument('--disable-notifications')  # Disable notifications
        # edge_options.add_argument('--disable-popup-blocking')  # Allow popups if needed

        # Create Edge WebDriver instance
        service = EdgeService(edge_driver_path)
        driver = webdriver.Edge(service=service, options=edge_options)

        return driver

    
    def get_firefox_driver(self):
        firefox_options = FirefoxOptions()
        if self.headless:
            firefox_options.add_argument("--headless")
        # firefox_options.add_argument("start-maximized")
        # firefox_options.add_argument("disable-infobars")
        # firefox_options.add_argument("--disable-extensions")
        # firefox_options.add_argument('--disable-gpu')
        # firefox_options.add_argument("--disable-dev-shm-usage")
        # firefox_options.add_argument("--window-size=800,600")
        # firefox_options.add_argument("--start-maximized")
        # firefox_options.set_preference('permissions.default.image',1)
        # firefox_options.set_preference("gfx.font_rendering.fontconfig.fontlist", "emoji font, sans-serif")
        # firefox_options.set_preference("font.name-list.emoji", "Noto Color Emoji")
        # firefox_options.set_preference("font.size.variable.emoji", 12)
        firefox_options.add_argument('--disable-application-cache')

        firefox_options.add_argument("--user-agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:96.0) Gecko/20100101 Firefox/96.0'")
        if os.name == "nt":
            _log_ok("Windows OS")
            profile_path =r"C:\Users\Dell\AppData\Roaming\Mozilla\Firefox\Profiles\0pf0oqk0.default-release"
            firefox_options.add_argument("-profile")     
            firefox_options.add_argument(profile_path)
            # executable_path ='geckodriver.exe'
        else:
            _log_ok("Linux OS")
            # profile_path = r"/home/dheerain/.mozilla/firefox/4c2e7nlw.default-release/"
            # To get the profile path  goto FireFox Menu> help>More troubleshooting Help
            profile_path = r"/home/dheerain/.mozilla/firefox/r6fkbovm.default-release"
            firefox_options.add_argument("-profile")
            firefox_options.add_argument(profile_path)
            executable_path ='./geckodriver_linux'
            firefox_options.set_preference("media.volume_scale", "0.0")
            
            firefox_options.set_preference('permissions.default.stylesheet', 2)
            ## Disable images
            firefox_options.set_preference('permissions.default.image', 2)
            ## Disable Flash
            firefox_options.set_preference('dom.ipc.plugins.enabled.libflashplayer.so','false')
        gecko_driver_path = os.path.join(os.getcwd(), "geckodriver.exe") 
        service = FirefoxService(gecko_driver_path)
        customDriver = webdriver.Firefox(service=service, options=firefox_options)
        return customDriver

    def send_message(self, contact_number, message, testmode=False):
        if testmode:
            contact_number = "Test Contact"

        chat_list_xpath = "//div[contains(@class,'fab-label') and normalize-space()='Start chat']"

        if not self.session:
            raise RuntimeError("No active session")

        chaticon = self.wait.until(EC.element_to_be_clickable((By.XPATH, chat_list_xpath)))
        chaticon.click()
        contact_textbox=self.wait.until(EC.element_to_be_clickable((By.XPATH, "//input[@placeholder='Type a name, phone number, or email']")))
        time.sleep(1)
        contact_textbox.click()
        contact_textbox.send_keys(contact_number)
        ActionChains(self.wait._driver).move_to_element(contact_textbox).send_keys(Keys.ENTER).perform()
        

        message_xpath = f"//textarea[contains(@placeholder,'RCS message')]"
        message_box = self.wait.until(EC.element_to_be_clickable((By.XPATH, message_xpath)))
        message_box.click()

        message_box.send_keys(message)
        
        ActionChains(self.wait._driver).move_to_element(message_box).send_keys(Keys.ENTER).perform()
        time.sleep(1)
        
          
        

if __name__ == "__main__":
    RCS_session = GoogleRCS(browser="chrome",headless=False)

    RCS_session.opensession()
    while True:
        RCS_session.send_message("+15197217740","Testing message")
        print("Dheerain")
