"""
Sports Betting Bot Configuration

Set these values via environment variables or a .env file.
"""
import os

# SportsLine credentials
SPORTSLINE_EMAIL = os.environ.get('SPORTSLINE_EMAIL', '')
SPORTSLINE_PASSWORD = os.environ.get('SPORTSLINE_PASSWORD', '')

# FanDuel credentials
FANDUEL_EMAIL = os.environ.get('FANDUEL_EMAIL', '')
FANDUEL_PASSWORD = os.environ.get('FANDUEL_PASSWORD', '')

# Betting settings
MAX_BET_AMOUNT = float(os.environ.get('MAX_BET_AMOUNT', '10.00'))
DEFAULT_BET_AMOUNT = float(os.environ.get('DEFAULT_BET_AMOUNT', '5.00'))
MIN_CONFIDENCE = float(os.environ.get('MIN_CONFIDENCE', '60.0'))  # minimum expert confidence % to auto-bet
DRY_RUN = os.environ.get('DRY_RUN', 'true').lower() == 'true'  # if True, log bets but don't place them

# Sports to track (comma-separated)
SPORTS = os.environ.get('SPORTS', 'nfl,nba,mlb,nhl').split(',')

# Refresh interval in minutes
REFRESH_INTERVAL = int(os.environ.get('REFRESH_INTERVAL', '30'))
