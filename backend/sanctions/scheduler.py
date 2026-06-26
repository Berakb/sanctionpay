"""
SanctionPay — Sanction List Scheduler
Listeleri düzenli aralıklarla günceller.
APScheduler kullanır, FastAPI ile birlikte çalışır.
"""

import logging
import asyncio
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from sanctions.fetcher import fetch_all_sources, fetch_source, SOURCES, init_db
import httpx

logger = logging.getLogger("sanctions.scheduler")


async def update_single_source(source_key: str):
    """Tek bir kaynağı güncelle (kaynak bazlı zamanlama için)."""
    async with httpx.AsyncClient(
        headers={"User-Agent": "SanctionPay/1.0"},
        timeout=90.0
    ) as client:
        count, status = await fetch_source(source_key, client)
        logger.info(f"Zamanlanmış güncelleme [{source_key}]: {count} kayıt, {status}")


def create_scheduler() -> AsyncIOScheduler:
    """Scheduler oluştur ve tüm kaynakları zamanla."""
    scheduler = AsyncIOScheduler(timezone="UTC")

    # Her kaynak kendi güncelleme sıklığına göre zamanlanır
    for source_key, cfg in SOURCES.items():
        hours = cfg.get("schedule_hours", 24)
        scheduler.add_job(
            update_single_source,
            trigger=IntervalTrigger(hours=hours),
            args=[source_key],
            id=f"update_{source_key}",
            name=f"Update {cfg['label']}",
            replace_existing=True,
        )
        logger.info(f"Zamanlandı: {source_key} — her {hours} saatte bir")

    return scheduler


async def startup_fetch():
    """Uygulama başlarken bir kez tüm listeleri çek."""
    logger.info("İlk sanction listesi güncellemesi başlıyor...")
    try:
        await fetch_all_sources()
        logger.info("İlk güncelleme tamamlandı.")
    except Exception as e:
        logger.error(f"İlk güncelleme hatası: {e}")
