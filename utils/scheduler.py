import schedule
import time
import logging
from datetime import datetime

logger = logging.getLogger(__name__)


def is_market_day():
    """Skip weekends."""
    return datetime.now().weekday() < 5  # Mon=0, Fri=4


def run_job(name: str, fn):
    """Wrapper to run any job safely with error handling."""
    if not is_market_day():
        logger.info(f"⏭️ Skipping {name} — weekend.")
        return
    try:
        logger.info(f"⏰ Running: {name}")
        fn()
    except Exception as e:
        logger.error(f"❌ Job '{name}' failed: {e}")


def start_scheduler(jobs: dict):
    """
    jobs = {
        "08:45": pre_market_scan,
        "09:30": market_open_scan,
        "15:30": end_of_day_report,
    }
    """
    for time_str, fn in jobs.items():
        name = fn.__name__
        schedule.every().day.at(time_str).do(run_job, name=name, fn=fn)
        logger.info(f"✅ Scheduled '{name}' at {time_str}")

    logger.info("🚀 Scheduler running... Press Ctrl+C to stop.")
    while True:
        schedule.run_pending()
        time.sleep(30)  # check every 30 seconds
