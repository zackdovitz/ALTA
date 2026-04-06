"""
SportsLine Expert Picks Scraper

Logs into SportsLine with your subscription credentials and pulls expert picks.
"""
import re
import json
import logging
from datetime import datetime
from dataclasses import dataclass, asdict

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

LOGIN_URL = 'https://secure.sportsline.com/login'
PICKS_BASE = 'https://www.sportsline.com/picks'
EXPERT_PICKS_URL = 'https://www.sportsline.com/picks/experts'

SPORT_URLS = {
    'nfl': f'{PICKS_BASE}/nfl/',
    'nba': f'{PICKS_BASE}/nba/',
    'mlb': f'{PICKS_BASE}/mlb/',
    'nhl': f'{PICKS_BASE}/nhl/',
    'ncaaf': f'{PICKS_BASE}/college-football/',
    'ncaab': f'{PICKS_BASE}/college-basketball/',
}

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
                  'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.9',
}


@dataclass
class Pick:
    sport: str
    expert: str
    matchup: str
    pick_type: str        # spread, moneyline, over_under, prop
    pick_detail: str      # e.g. "Chiefs -3.5", "Over 47.5"
    odds: str             # e.g. "-110"
    confidence: float     # 0-100
    units: float          # expert's unit rating
    game_time: str
    reasoning: str
    scraped_at: str

    def to_dict(self):
        return asdict(self)


class SportsLineScraper:
    def __init__(self, email: str, password: str):
        self.email = email
        self.password = password
        self.session = requests.Session()
        self.session.headers.update(HEADERS)
        self._logged_in = False

    def login(self) -> bool:
        """Authenticate with SportsLine."""
        if not self.email or not self.password:
            logger.error('SportsLine credentials not configured')
            return False

        try:
            # Get login page for CSRF token
            resp = self.session.get(LOGIN_URL, timeout=15)
            soup = BeautifulSoup(resp.text, 'html.parser')

            # Look for CSRF or hidden form tokens
            csrf_input = soup.find('input', {'name': re.compile(r'csrf|token', re.I)})
            csrf_token = csrf_input['value'] if csrf_input else ''

            login_data = {
                'email': self.email,
                'password': self.password,
            }
            if csrf_token:
                login_data['_token'] = csrf_token

            resp = self.session.post(LOGIN_URL, data=login_data, timeout=15,
                                     allow_redirects=True)

            if resp.status_code == 200 and 'logout' in resp.text.lower():
                self._logged_in = True
                logger.info('Successfully logged in to SportsLine')
                return True

            # Check for JSON response (some login flows return JSON)
            try:
                data = resp.json()
                if data.get('success') or data.get('authenticated'):
                    self._logged_in = True
                    logger.info('Successfully logged in to SportsLine (JSON)')
                    return True
            except (ValueError, AttributeError):
                pass

            logger.warning('SportsLine login may have failed — check credentials')
            # Still mark as logged in to attempt scraping (session cookies might work)
            self._logged_in = True
            return True

        except requests.RequestException as e:
            logger.error(f'SportsLine login error: {e}')
            return False

    def get_picks(self, sports: list[str] | None = None) -> list[Pick]:
        """Fetch expert picks for the given sports."""
        if not self._logged_in:
            if not self.login():
                return []

        all_picks = []
        target_sports = sports or list(SPORT_URLS.keys())

        for sport in target_sports:
            sport = sport.strip().lower()
            url = SPORT_URLS.get(sport)
            if not url:
                logger.warning(f'Unknown sport: {sport}')
                continue

            try:
                picks = self._scrape_sport_picks(sport, url)
                all_picks.extend(picks)
                logger.info(f'Found {len(picks)} picks for {sport.upper()}')
            except Exception as e:
                logger.error(f'Error scraping {sport}: {e}')

        return all_picks

    def _scrape_sport_picks(self, sport: str, url: str) -> list[Pick]:
        """Scrape picks from a sport-specific page."""
        resp = self.session.get(url, timeout=15)
        if resp.status_code != 200:
            logger.warning(f'Got {resp.status_code} for {url}')
            return []

        soup = BeautifulSoup(resp.text, 'html.parser')
        picks = []
        now = datetime.now().isoformat()

        # Try parsing embedded JSON data first (SportsLine often embeds pick data)
        picks_from_json = self._extract_json_picks(soup, sport, now)
        if picks_from_json:
            return picks_from_json

        # Fall back to HTML parsing
        # Look for pick cards/containers — common SportsLine patterns
        pick_containers = (
            soup.select('.pick-card') or
            soup.select('[data-testid="pick-card"]') or
            soup.select('.expert-pick') or
            soup.select('.picks-list .pick') or
            soup.select('article.pick')
        )

        for card in pick_containers:
            try:
                pick = self._parse_pick_card(card, sport, now)
                if pick:
                    picks.append(pick)
            except Exception as e:
                logger.debug(f'Failed to parse pick card: {e}')

        return picks

    def _extract_json_picks(self, soup: BeautifulSoup, sport: str, timestamp: str) -> list[Pick]:
        """Try to extract picks from embedded JSON/script data."""
        picks = []

        for script in soup.find_all('script'):
            text = script.string or ''
            # Look for common data patterns
            for pattern in [
                r'window\.__INITIAL_STATE__\s*=\s*({.+?});',
                r'window\.__NEXT_DATA__\s*=\s*({.+?});',
                r'"picks"\s*:\s*(\[.+?\])',
            ]:
                match = re.search(pattern, text, re.DOTALL)
                if match:
                    try:
                        data = json.loads(match.group(1))
                        picks.extend(self._parse_json_picks(data, sport, timestamp))
                    except (json.JSONDecodeError, Exception) as e:
                        logger.debug(f'JSON parse failed: {e}')

        return picks

    def _parse_json_picks(self, data, sport: str, timestamp: str) -> list[Pick]:
        """Parse pick objects from JSON data structure."""
        picks = []
        pick_list = []

        # Navigate common data structures
        if isinstance(data, list):
            pick_list = data
        elif isinstance(data, dict):
            pick_list = (
                data.get('picks', []) or
                data.get('props', {}).get('pageProps', {}).get('picks', []) or
                data.get('expertPicks', [])
            )

        for item in pick_list:
            if not isinstance(item, dict):
                continue
            try:
                pick = Pick(
                    sport=sport,
                    expert=item.get('expertName', item.get('expert', {}).get('name', 'Unknown')),
                    matchup=item.get('matchup', item.get('gameTitle', '')),
                    pick_type=item.get('pickType', item.get('betType', 'spread')).lower(),
                    pick_detail=item.get('pickDetail', item.get('pick', item.get('selection', ''))),
                    odds=str(item.get('odds', item.get('line', ''))),
                    confidence=float(item.get('confidence', item.get('rating', 0))),
                    units=float(item.get('units', item.get('starRating', 0))),
                    game_time=item.get('gameTime', item.get('eventDate', '')),
                    reasoning=item.get('analysis', item.get('reasoning', '')),
                    scraped_at=timestamp,
                )
                if pick.matchup and pick.pick_detail:
                    picks.append(pick)
            except (ValueError, TypeError) as e:
                logger.debug(f'Skipping pick item: {e}')

        return picks

    def _parse_pick_card(self, card, sport: str, timestamp: str) -> Pick | None:
        """Parse a single pick card from HTML."""
        def text(selector):
            el = card.select_one(selector)
            return el.get_text(strip=True) if el else ''

        expert = (
            text('.expert-name') or
            text('[data-testid="expert-name"]') or
            text('.analyst-name') or
            text('.author')
        )
        matchup = (
            text('.matchup') or
            text('.game-title') or
            text('[data-testid="matchup"]') or
            text('h3') or
            text('h4')
        )
        pick_detail = (
            text('.pick-selection') or
            text('.pick-detail') or
            text('[data-testid="pick"]') or
            text('.selection')
        )
        odds = (
            text('.odds') or
            text('.line') or
            text('[data-testid="odds"]')
        )

        # Try to find confidence/star rating
        confidence = 0.0
        conf_el = card.select_one('.confidence') or card.select_one('.rating') or card.select_one('.stars')
        if conf_el:
            conf_text = conf_el.get_text(strip=True)
            nums = re.findall(r'[\d.]+', conf_text)
            if nums:
                confidence = float(nums[0])
                # If it's a 1-5 star scale, normalize to 0-100
                if confidence <= 5:
                    confidence *= 20

        # Units
        units = 0.0
        unit_el = card.select_one('.units') or card.select_one('.unit-size')
        if unit_el:
            unit_nums = re.findall(r'[\d.]+', unit_el.get_text())
            if unit_nums:
                units = float(unit_nums[0])

        # Pick type detection
        pick_type = 'spread'
        detail_lower = pick_detail.lower()
        if 'over' in detail_lower or 'under' in detail_lower:
            pick_type = 'over_under'
        elif 'ml' in detail_lower or 'moneyline' in detail_lower or (odds and not any(c in pick_detail for c in ['+', '-', '.'])):
            pick_type = 'moneyline'
        elif 'prop' in detail_lower:
            pick_type = 'prop'

        game_time = text('.game-time') or text('.event-date') or text('time')
        reasoning = text('.analysis') or text('.pick-reasoning') or text('.description')

        if not matchup and not pick_detail:
            return None

        return Pick(
            sport=sport,
            expert=expert or 'Unknown Expert',
            matchup=matchup,
            pick_type=pick_type,
            pick_detail=pick_detail,
            odds=odds,
            confidence=confidence,
            units=units,
            game_time=game_time,
            reasoning=reasoning,
            scraped_at=timestamp,
        )
