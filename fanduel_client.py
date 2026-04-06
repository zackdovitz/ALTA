"""
FanDuel Bet Placement Client

Uses Selenium to automate bet placement on FanDuel Sportsbook.

WARNING: Automated betting may violate FanDuel's Terms of Service.
Use at your own risk. DRY_RUN mode is enabled by default.
"""
import re
import time
import logging
from dataclasses import dataclass, asdict
from datetime import datetime

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, NoSuchElementException

logger = logging.getLogger(__name__)

FANDUEL_BASE = 'https://sportsbook.fanduel.com'
FANDUEL_LOGIN = 'https://account.fanduel.com/login'


@dataclass
class BetResult:
    success: bool
    pick_detail: str
    amount: float
    odds: str
    potential_payout: float
    confirmation_id: str
    error: str
    timestamp: str
    dry_run: bool

    def to_dict(self):
        return asdict(self)


class FanDuelClient:
    def __init__(self, email: str, password: str, dry_run: bool = True, headless: bool = True):
        self.email = email
        self.password = password
        self.dry_run = dry_run
        self.headless = headless
        self.driver = None
        self._logged_in = False

    def _init_driver(self):
        """Initialize Chrome WebDriver."""
        if self.driver:
            return

        options = webdriver.ChromeOptions()
        if self.headless:
            options.add_argument('--headless=new')
        options.add_argument('--no-sandbox')
        options.add_argument('--disable-dev-shm-usage')
        options.add_argument('--disable-blink-features=AutomationControlled')
        options.add_argument('--window-size=1920,1080')
        options.add_argument(
            'user-agent=Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
            'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        )
        # Reduce automation detection
        options.add_experimental_option('excludeSwitches', ['enable-automation'])
        options.add_experimental_option('useAutomationExtension', False)

        self.driver = webdriver.Chrome(options=options)
        self.driver.execute_cdp_cmd('Page.addScriptToEvaluateOnNewDocument', {
            'source': 'Object.defineProperty(navigator, "webdriver", {get: () => undefined})'
        })

    def login(self) -> bool:
        """Log into FanDuel Sportsbook."""
        if not self.email or not self.password:
            logger.error('FanDuel credentials not configured')
            return False

        self._init_driver()

        try:
            self.driver.get(FANDUEL_LOGIN)
            wait = WebDriverWait(self.driver, 15)

            # Enter email
            email_field = wait.until(
                EC.presence_of_element_located((By.CSS_SELECTOR, 'input[type="email"], input[name="email"], #email'))
            )
            email_field.clear()
            email_field.send_keys(self.email)

            # Enter password
            pass_field = self.driver.find_element(By.CSS_SELECTOR, 'input[type="password"], input[name="password"], #password')
            pass_field.clear()
            pass_field.send_keys(self.password)

            # Submit
            pass_field.send_keys(Keys.RETURN)

            # Wait for redirect to sportsbook
            time.sleep(3)
            wait.until(lambda d: 'sportsbook' in d.current_url or 'account' in d.current_url)

            self._logged_in = True
            logger.info('Successfully logged in to FanDuel')
            return True

        except TimeoutException:
            logger.error('FanDuel login timed out — check credentials or 2FA requirements')
            return False
        except Exception as e:
            logger.error(f'FanDuel login error: {e}')
            return False

    def get_balance(self) -> float | None:
        """Get current FanDuel account balance."""
        if not self._logged_in:
            return None

        try:
            self.driver.get(FANDUEL_BASE)
            time.sleep(2)
            # Look for balance element
            balance_el = self.driver.find_element(
                By.CSS_SELECTOR, '[data-testid="balance"], .balance-amount, .account-balance'
            )
            balance_text = balance_el.text.strip()
            amount = re.search(r'[\d,.]+', balance_text)
            if amount:
                return float(amount.group().replace(',', ''))
        except Exception as e:
            logger.debug(f'Could not fetch balance: {e}')
        return None

    def search_and_select_bet(self, matchup: str, pick_detail: str, pick_type: str) -> bool:
        """Navigate to a game and add a bet to the betslip."""
        try:
            # Search for the matchup
            self.driver.get(FANDUEL_BASE)
            time.sleep(2)

            # Use FanDuel search
            search_btn = self.driver.find_element(
                By.CSS_SELECTOR, '[data-testid="search"], .search-icon, button[aria-label="Search"]'
            )
            search_btn.click()
            time.sleep(1)

            search_input = WebDriverWait(self.driver, 10).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, 'input[type="search"], input[placeholder*="Search"]'))
            )

            # Extract team name from matchup for search
            teams = re.split(r'\s+(?:vs\.?|@|at)\s+', matchup, flags=re.I)
            search_term = teams[0].strip() if teams else matchup
            search_input.send_keys(search_term)
            time.sleep(2)

            # Click the matching game result
            results = self.driver.find_elements(By.CSS_SELECTOR, '.search-result, [data-testid="search-result"]')
            for result in results:
                if any(team.strip().lower() in result.text.lower() for team in teams):
                    result.click()
                    time.sleep(2)
                    break
            else:
                # Try clicking first result
                if results:
                    results[0].click()
                    time.sleep(2)
                else:
                    logger.warning(f'No search results for: {matchup}')
                    return False

            # Find and click the matching bet option
            return self._select_bet_option(pick_detail, pick_type)

        except Exception as e:
            logger.error(f'Error finding bet for {matchup}: {e}')
            return False

    def _select_bet_option(self, pick_detail: str, pick_type: str) -> bool:
        """Click the correct bet option on the game page."""
        try:
            # Look for spread/moneyline/total tabs
            tab_map = {
                'spread': ['Spread', 'Point Spread'],
                'moneyline': ['Moneyline', 'Money Line'],
                'over_under': ['Total', 'Over/Under', 'Totals'],
            }

            tab_names = tab_map.get(pick_type, [])
            for tab_name in tab_names:
                try:
                    tabs = self.driver.find_elements(By.XPATH, f'//*[contains(text(), "{tab_name}")]')
                    for tab in tabs:
                        if tab.is_displayed():
                            tab.click()
                            time.sleep(1)
                            break
                except Exception:
                    pass

            # Find all clickable odds/bet buttons
            bet_buttons = self.driver.find_elements(
                By.CSS_SELECTOR,
                '[role="button"][data-testid*="outcome"], '
                '.outcome-cell, .odds-button, '
                '[class*="outcome"], [class*="odds"]'
            )

            pick_lower = pick_detail.lower()
            for btn in bet_buttons:
                btn_text = btn.text.lower()
                # Match team name or over/under
                if self._pick_matches_button(pick_lower, btn_text):
                    btn.click()
                    time.sleep(1)
                    logger.info(f'Selected bet: {btn.text}')
                    return True

            logger.warning(f'Could not find matching bet button for: {pick_detail}')
            return False

        except Exception as e:
            logger.error(f'Error selecting bet option: {e}')
            return False

    def _pick_matches_button(self, pick_lower: str, btn_text: str) -> bool:
        """Check if a pick description matches a bet button's text."""
        # Extract key parts of the pick
        words = pick_lower.split()
        if not words:
            return False

        # For over/under
        if 'over' in pick_lower and 'over' in btn_text:
            return True
        if 'under' in pick_lower and 'under' in btn_text:
            return True

        # For team picks — check if team name is in button
        team_name = words[0]
        if len(team_name) > 2 and team_name in btn_text:
            return True

        return False

    def place_bet(self, matchup: str, pick_detail: str, pick_type: str,
                  amount: float, odds: str = '') -> BetResult:
        """Place a single bet on FanDuel."""
        now = datetime.now().isoformat()

        if self.dry_run:
            logger.info(f'[DRY RUN] Would bet ${amount:.2f} on {pick_detail} ({matchup})')
            return BetResult(
                success=True,
                pick_detail=pick_detail,
                amount=amount,
                odds=odds,
                potential_payout=0.0,
                confirmation_id='DRY_RUN',
                error='',
                timestamp=now,
                dry_run=True,
            )

        if not self._logged_in:
            if not self.login():
                return BetResult(False, pick_detail, amount, odds, 0.0, '', 'Login failed', now, False)

        try:
            # Search for game and select bet
            if not self.search_and_select_bet(matchup, pick_detail, pick_type):
                return BetResult(False, pick_detail, amount, odds, 0.0, '',
                                 'Could not find bet', now, False)

            # Enter wager amount in betslip
            wager_input = WebDriverWait(self.driver, 10).until(
                EC.presence_of_element_located((
                    By.CSS_SELECTOR,
                    'input[data-testid="betslip-wager"], '
                    'input[aria-label*="Wager"], '
                    'input[placeholder*="Wager"], '
                    '.betslip input[type="text"], '
                    '.betslip input[type="number"]'
                ))
            )
            wager_input.clear()
            wager_input.send_keys(str(amount))
            time.sleep(1)

            # Get potential payout
            payout = 0.0
            try:
                payout_el = self.driver.find_element(
                    By.CSS_SELECTOR, '[data-testid="potential-payout"], .potential-win, .to-win'
                )
                payout_nums = re.findall(r'[\d,.]+', payout_el.text)
                if payout_nums:
                    payout = float(payout_nums[0].replace(',', ''))
            except Exception:
                pass

            # Click place bet button
            place_btn = WebDriverWait(self.driver, 10).until(
                EC.element_to_be_clickable((
                    By.CSS_SELECTOR,
                    'button[data-testid="place-bet"], '
                    'button[data-testid="betslip-submit"], '
                    'button.place-bet-btn'
                ))
            )
            place_btn.click()
            time.sleep(3)

            # Check for confirmation
            try:
                conf_el = WebDriverWait(self.driver, 10).until(
                    EC.presence_of_element_located((
                        By.CSS_SELECTOR,
                        '[data-testid="bet-confirmation"], '
                        '.bet-receipt, .confirmation'
                    ))
                )
                conf_text = conf_el.text
                conf_id = re.search(r'(?:confirmation|receipt|bet)\s*#?\s*([\w-]+)', conf_text, re.I)
                confirmation_id = conf_id.group(1) if conf_id else 'CONFIRMED'
            except TimeoutException:
                confirmation_id = 'PLACED_NO_CONFIRM'

            logger.info(f'Bet placed: ${amount:.2f} on {pick_detail} — {confirmation_id}')
            return BetResult(True, pick_detail, amount, odds, payout, confirmation_id, '', now, False)

        except Exception as e:
            logger.error(f'Error placing bet: {e}')
            return BetResult(False, pick_detail, amount, odds, 0.0, '', str(e), now, False)

    def close(self):
        """Clean up browser."""
        if self.driver:
            try:
                self.driver.quit()
            except Exception:
                pass
            self.driver = None
            self._logged_in = False
