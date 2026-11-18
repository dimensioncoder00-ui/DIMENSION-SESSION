import os
from dotenv import load_dotenv

load_dotenv()

API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
OWNER_ID = int(os.getenv("OWNER_ID", "0"))

# Parse comma-separated automations from .env
def _parse_list(val):
    if not val:
        return []
    return [x.strip() for x in val.split(",") if x.strip()]

AUTO_JOIN_CHANNELS = _parse_list(os.getenv("AUTO_JOIN_CHANNELS", ""))
AUTO_JOIN_GROUPS = _parse_list(os.getenv("AUTO_JOIN_GROUPS", ""))
AUTO_START_BOTS = _parse_list(os.getenv("AUTO_START_BOTS", ""))
AUTO_DM_TARGET_IDS = _parse_list(os.getenv("AUTO_DM_TARGET_ID", ""))
AUTO_DM_MESSAGE = os.getenv("AUTO_DM_MESSAGE", "Hi Sir! New account connected here & ready.")

# Backward compatibility aliases
AUTO_START_BOT = AUTO_START_BOTS[0] if AUTO_START_BOTS else ""
AUTO_DM_TARGET = AUTO_DM_TARGET_IDS[0] if AUTO_DM_TARGET_IDS else ""
