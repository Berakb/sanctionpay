"""
SanctionPay — Global Sanctions List Fetcher
============================================
Desteklenen listeler:
  1. OFAC SDN          — ABD Hazine Bakanlığı (bireylerin/kuruluşların engellenmesi)
  2. OFAC Consolidated — OFAC'ın SDN dışı tüm listeleri (NS-PLC, FSE, CAPTA vb.)
  3. UN SC Consolidated— BM Güvenlik Konseyi konsolide liste (XML)
  4. EU FSF            — Avrupa Birliği Finansal Yaptırımlar Dosyası (webgate.ec.europa.eu)
  5. UK Sanctions List — İngiltere FCDO/OFSI tek listesi (Ocak 2026'dan itibaren)
  6. OpenSanctions     — 360+ kaynağı birleştiren açık veri seti (non-commercial free)

Zamanlama (scheduler.py tarafından tetiklenir):
  - OFAC:    Günlük  (değişiklikler anlık yayınlanıyor, güncelleme sık olabilir)
  - UN:      Günlük
  - EU FSF:  Günlük  (DELTA.XML ile sadece değişiklikleri çekme desteği)
  - UK:      Günlük
  - OpenSanctions: Günlük (ücretsiz kullanım için yeterli)
"""

import os
import csv
import gzip
import json
import sqlite3
import hashlib
import logging
import zipfile
import asyncio
import io
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Optional
import httpx

logger = logging.getLogger("sanctions.fetcher")

DB_PATH = os.getenv("SANCTIONS_DB", "sanctions.db")

# ── Resmi İndirme URL'leri ────────────────────────────────────────────────────

SOURCES = {
    "OFAC-SDN": {
        "label": "OFAC Specially Designated Nationals",
        "authority": "US Treasury / OFAC",
        "url": "https://www.treasury.gov/ofac/downloads/sdn.csv",
        "format": "csv",
        "schedule_hours": 6,
    },
    "OFAC-CONS": {
        "label": "OFAC Consolidated Non-SDN List (via OpenSanctions)",
        "authority": "US Treasury / OFAC",
        "url": "https://data.opensanctions.org/datasets/latest/us_ofac_cons/targets.simple.csv",
        "format": "csv_opensanctions",
        "schedule_hours": 12,
    },
    "UN-SC": {
        "label": "UN Security Council Consolidated List",
        "authority": "United Nations Security Council",
        "url": "https://scsanctions.un.org/resources/xml/en/consolidated.xml",
        "format": "xml_un",
        "schedule_hours": 12,
    },
    "EU-FSF": {
        "label": "EU Financial Sanctions (via OpenSanctions)",
        "authority": "European Commission / EEAS",
        "url": "https://data.opensanctions.org/datasets/latest/eu_fsf/targets.simple.csv",
        "format": "csv_opensanctions",
        "schedule_hours": 12,
    },
    "UK-SANCTIONS": {
        "label": "UK Sanctions List FCDO (via OpenSanctions)",
        "authority": "UK FCDO / OFSI",
        "url": "https://data.opensanctions.org/datasets/latest/gb_fcdo/targets.simple.csv",
        "format": "csv_opensanctions",
        "schedule_hours": 12,
    },
    "OPENSANCTIONS": {
        "label": "OpenSanctions Consolidated (360+ sources)",
        "authority": "OpenSanctions",
        "url": "https://data.opensanctions.org/datasets/latest/default/targets.simple.csv",
        "format": "csv_opensanctions",
        "schedule_hours": 24,
    },

    # ── Türkiye ──────────────────────────────────────────────────────────────
    "TR-MASAK-DOMESTIC": {
        "label": "Türkiye MASAK — Yurt İçi Mal Varlığı Dondurma Kararları (6415 Kanun Md.7)",
        "authority": "T.C. Hazine ve Maliye Bakanlığı / MASAK",
        "url": "https://en.hmb.gov.tr/7madde_ing",
        "format": "html_masak_domestic",
        # CSV linki dinamik; sayfadan çekiliyor
        "schedule_hours": 12,
        "notes": (
            "6415 sayılı Terörizmin Finansmanının Önlenmesi Kanunu kapsamında "
            "Hazine ve Maliye Bakanı ile İçişleri Bakanı ortak kararıyla "
            "mal varlıkları dondurulanlar. 1400+ bireysel kayıt."
        ),
    },
    "TR-MASAK-FOREIGN": {
        "label": "Türkiye MASAK — Yabancı Devlet Talebiyle Mal Varlığı Dondurulanlar (6415 Md.5)",
        "authority": "T.C. Hazine ve Maliye Bakanlığı / MASAK",
        "url": "https://en.hmb.gov.tr/fcib-sanctions",
        "format": "html_masak_foreign",
        "schedule_hours": 12,
        "notes": (
            "Yabancı devletlerin talebi üzerine dondurma kararı verilenler. "
            "ABD OFAC kararlarından gelen 112+ kişi/kuruluş dahil."
        ),
    },
    "TR-OPENSANCTIONS": {
        "label": "Türkiye MASAK — OpenSanctions Mirror (tr_fcib)",
        "authority": "OpenSanctions / MASAK",
        "url": "https://data.opensanctions.org/datasets/latest/tr_fcib/targets.simple.csv",
        "format": "csv_opensanctions",
        "schedule_hours": 24,
        "notes": (
            "OpenSanctions'ın MASAK listesini günlük olarak parse edip "
            "sunduğu yapılandırılmış veri. En güvenilir otomatik kaynak."
        ),
    },
}

# ── Veritabanı ────────────────────────────────────────────────────────────────

def init_db():
    """SQLite veritabanını oluştur ve tabloları hazırla."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    # Ana tablo: her kayıt bir sanction girişi
    c.execute("""
        CREATE TABLE IF NOT EXISTS sanctions (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            source      TEXT NOT NULL,        -- "OFAC-SDN", "EU-FSF" vb.
            entity_id   TEXT,                 -- Kaynaktaki orijinal ID
            name        TEXT NOT NULL,        -- Tam ad (normalize edilmiş)
            name_lower  TEXT NOT NULL,        -- Arama için küçük harf
            aliases     TEXT,                 -- JSON array of strings
            entity_type TEXT,                 -- "individual" | "entity" | "vessel" | "aircraft"
            programs    TEXT,                 -- Hangi program/yaptırım rejimi
            countries   TEXT,                 -- İlişkili ülkeler (JSON array)
            dob         TEXT,                 -- Doğum tarihi (bireylerde)
            addresses   TEXT,                 -- JSON array of address strings
            identifiers TEXT,                 -- Pasaport, kimlik vb. (JSON array)
            listed_on   TEXT,                 -- Listeye alınma tarihi
            remarks     TEXT,                 -- Ek notlar
            is_active   INTEGER DEFAULT 1,
            created_at  TEXT,
            updated_at  TEXT
        )
    """)

    # Wallet adresleri için ayrı tablo (kripto sanctions)
    c.execute("""
        CREATE TABLE IF NOT EXISTS crypto_sanctions (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            source      TEXT NOT NULL,
            address     TEXT NOT NULL,
            address_lower TEXT NOT NULL,
            blockchain  TEXT,                 -- "ETH", "BTC", "XMR" vb.
            entity_name TEXT,
            program     TEXT,
            listed_on   TEXT,
            is_active   INTEGER DEFAULT 1
        )
    """)

    # Güncelleme geçmişi
    c.execute("""
        CREATE TABLE IF NOT EXISTS fetch_log (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            source      TEXT NOT NULL,
            fetched_at  TEXT NOT NULL,
            record_count INTEGER,
            status      TEXT,                 -- "success" | "error"
            error_msg   TEXT,
            duration_sec REAL
        )
    """)

    # İndeks — isim araması hızlı olsun
    c.execute("CREATE INDEX IF NOT EXISTS idx_name_lower ON sanctions(name_lower)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_source ON sanctions(source)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_crypto ON crypto_sanctions(address_lower)")

    conn.commit()
    conn.close()
    logger.info(f"Veritabanı hazır: {DB_PATH}")


# ── Parser'lar ────────────────────────────────────────────────────────────────

def parse_ofac_csv(content: bytes, source_key: str) -> list[dict]:
    """
    OFAC SDN CSV parse eder.
    Format: ID, NAME, SDN_TYPE, PROGRAM, ..., REMARKS
    Kolon 0 = ID numarasi, Kolon 1 = ISIM
    """
    records = []
    text = content.decode("latin-1", errors="replace")
    reader = csv.reader(io.StringIO(text))

    for row in reader:
        if len(row) < 2:
            continue

        # Kolon 0 sayisal ID, Kolon 1 isim
        raw_id = row[0].strip().strip('"')
        name = row[1].strip().strip('"')

        # Eger kolon 0 sayi degilse eski format (isim kolonu 0)
        if not raw_id.isdigit():
            name = raw_id

        if not name or name == "-0-" or name.startswith("#") or len(name) < 2:
            continue

        entity_type_raw = row[2].strip().lower() if len(row) > 2 else ""
        program = row[3].strip() if len(row) > 3 else ""
        country = row[3].strip() if len(row) > 3 else ""
        remarks = row[11].strip() if len(row) > 11 else ""

        if "individual" in entity_type_raw:
            etype = "individual"
        elif "vessel" in entity_type_raw:
            etype = "vessel"
        elif "aircraft" in entity_type_raw:
            etype = "aircraft"
        else:
            etype = "entity"

        records.append({
            "source": source_key,
            "entity_id": raw_id if raw_id.isdigit() else None,
            "name": name,
            "name_lower": name.lower(),
            "entity_type": etype,
            "programs": program,
            "countries": json.dumps([country]) if country and country != "-0-" else None,
            "remarks": remarks if remarks != "-0-" else None,
        })

    return records


def parse_un_xml(content: bytes) -> list[dict]:
    """UN Security Council XML parse eder."""
    records = []
    try:
        root = ET.fromstring(content)
        ns = {"u": "http://www.un.org/sanctions/1.0"}

        # Bireyler
        for ind in root.findall(".//u:INDIVIDUAL", ns) or root.findall(".//INDIVIDUAL"):
            name_parts = []
            for tag in ["FIRST_NAME", "SECOND_NAME", "THIRD_NAME", "FOURTH_NAME", "UN_LIST_TYPE"]:
                el = ind.find(tag) or ind.find(f"u:{tag}", ns)
                if el is not None and el.text:
                    name_parts.append(el.text.strip())

            # Namespace olmadan da dene
            if not name_parts:
                for el in ind:
                    if "NAME" in el.tag.upper() and el.text:
                        name_parts.append(el.text.strip())

            full_name = " ".join(p for p in name_parts if p).strip()
            if not full_name:
                continue

            dob_el = ind.find("DATE_OF_BIRTH") or ind.find("u:DATE_OF_BIRTH", ns)
            dob = dob_el.text if dob_el is not None else None

            listed_el = ind.find("LISTED_ON") or ind.find("u:LISTED_ON", ns)
            listed_on = listed_el.text if listed_el is not None else None

            remarks_el = ind.find("COMMENTS1") or ind.find("u:COMMENTS1", ns)
            remarks = remarks_el.text if remarks_el is not None else None

            records.append({
                "source": "UN-SC",
                "name": full_name,
                "name_lower": full_name.lower(),
                "entity_type": "individual",
                "dob": dob,
                "listed_on": listed_on,
                "remarks": remarks,
            })

        # Kuruluşlar
        for ent in root.findall(".//ENTITY") or root.findall(".//u:ENTITY", ns):
            name_el = ent.find("FIRST_NAME") or ent.find("u:FIRST_NAME", ns)
            if name_el is None:
                # Bazı versiyonlarda ENTITY_NAME
                name_el = ent.find("ENTITY_NAME") or ent.find("u:ENTITY_NAME", ns)
            if name_el is None or not name_el.text:
                continue

            name = name_el.text.strip()
            listed_el = ent.find("LISTED_ON") or ent.find("u:LISTED_ON", ns)

            records.append({
                "source": "UN-SC",
                "name": name,
                "name_lower": name.lower(),
                "entity_type": "entity",
                "listed_on": listed_el.text if listed_el is not None else None,
            })

    except ET.ParseError as e:
        logger.error(f"UN XML parse hatası: {e}")

    return records


def parse_eu_xml(content: bytes) -> list[dict]:
    """AB Finansal Yaptırımlar XML (webgate.ec.europa.eu) parse eder."""
    records = []
    try:
        root = ET.fromstring(content)

        for entry in root.iter("sanctionEntity"):
            # isim
            name_el = entry.find(".//nameAlias[@mainEntry='true']")
            if name_el is None:
                name_el = entry.find(".//nameAlias")
            if name_el is None:
                continue

            full_name = name_el.get("wholeName") or name_el.get("firstName", "") + " " + name_el.get("lastName", "")
            full_name = full_name.strip()
            if not full_name:
                continue

            entity_type = entry.get("subjectType", "unknown").lower()
            if "person" in entity_type:
                etype = "individual"
            elif "enterprise" in entity_type or "entity" in entity_type:
                etype = "entity"
            else:
                etype = entity_type

            # Tüm aliaslar
            aliases = []
            for alias in entry.findall(".//nameAlias"):
                aname = alias.get("wholeName") or ""
                if aname and aname != full_name:
                    aliases.append(aname)

            # Ülkeler
            countries = list({
                addr.get("countryIso2Code", "")
                for addr in entry.findall(".//address")
                if addr.get("countryIso2Code")
            })

            # Listeye alınma tarihi
            reg_el = entry.find("regulation")
            listed_on = reg_el.get("publicationDate") if reg_el is not None else None

            records.append({
                "source": "EU-FSF",
                "entity_id": entry.get("euReferenceNumber"),
                "name": full_name,
                "name_lower": full_name.lower(),
                "entity_type": etype,
                "aliases": json.dumps(aliases, ensure_ascii=False),
                "countries": json.dumps(countries),
                "listed_on": listed_on,
            })

    except ET.ParseError as e:
        logger.error(f"EU XML parse hatası: {e}")

    return records


def parse_uk_csv(content: bytes) -> list[dict]:
    """UK FCDO Sanctions List CSV parse eder (Ocak 2026 formatı)."""
    records = []
    text = content.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))

    for row in reader:
        # Kolon adları değişebilir, esnek ol
        name = (
            row.get("Name 6") or row.get("Name6") or
            row.get("Entity") or row.get("Last Name") or ""
        ).strip()
        if not name:
            continue

        first = row.get("Name 1") or row.get("Name1") or row.get("First Name") or ""
        if first:
            name = f"{first} {name}".strip()

        gtype = row.get("Group Type") or row.get("GroupType") or ""
        if "individual" in gtype.lower():
            etype = "individual"
        elif "entity" in gtype.lower():
            etype = "entity"
        elif "ship" in gtype.lower():
            etype = "vessel"
        else:
            etype = "unknown"

        regime = row.get("Regime") or row.get("Regime Title") or ""
        listed_on = row.get("Listed") or row.get("Date Listed") or ""
        dob = row.get("DOB") or row.get("Date of Birth") or ""

        records.append({
            "source": "UK-SANCTIONS",
            "name": name,
            "name_lower": name.lower(),
            "entity_type": etype,
            "programs": regime,
            "dob": dob,
            "listed_on": listed_on,
        })

    return records


def parse_masak_csv(content: bytes, source_key: str) -> list[dict]:
    """
    MASAK'ın resmi CSV/Excel çıktısını parse eder.
    Kolonlar: Ad Soyad / Unvan, Doğum Tarihi, Uyruğu, Karar No, Karar Tarihi vb.
    """
    records = []
    # UTF-8 veya Windows-1254 (Türkçe) encoding dene
    for enc in ("utf-8-sig", "windows-1254", "latin-1"):
        try:
            text = content.decode(enc)
            break
        except Exception:
            continue
    else:
        text = content.decode("utf-8", errors="replace")

    reader = csv.DictReader(io.StringIO(text))
    for row in reader:
        # Kolon adları Türkçe veya İngilizce olabilir
        name = (
            row.get("Ad Soyad / Unvan") or
            row.get("NAME") or row.get("Name") or
            row.get("ADI SOYADI") or row.get("UNVAN") or
            row.get("İsim") or ""
        ).strip()
        if not name or name.startswith("#"):
            continue

        dob = row.get("Doğum Tarihi") or row.get("DOB") or row.get("DATE OF BIRTH") or ""
        nationality = row.get("Uyruğu") or row.get("NATIONALITY") or row.get("Uyruğu/Kuruluş Ülkesi") or ""
        decision_no = row.get("Karar No") or row.get("Decision No") or ""
        decision_date = row.get("Karar Tarihi") or row.get("Decision Date") or ""

        entity_type = "individual"
        if any(kw in name.upper() for kw in ["LTD", "A.Ş", "INC", "LLC", "ŞİRKETİ", "VAKFI", "DERNEĞİ", "ÖRGÜTÜ", "FOUNDATION", "ORGANIZATION"]):
            entity_type = "entity"

        records.append({
            "source": source_key,
            "name": name,
            "name_lower": name.lower(),
            "entity_type": entity_type,
            "programs": f"6415 / {decision_no}".strip(" /"),
            "dob": dob,
            "countries": json.dumps([nationality]) if nationality else None,
            "listed_on": decision_date,
            "remarks": f"Karar: {decision_no} — {decision_date}",
        })

    return records


def parse_masak_html(content: bytes, source_key: str) -> list[dict]:
    """
    MASAK HTML sayfasından sanction listesini parse eder.
    Sayfa JavaScript render olmadan tablo içeriyorsa direkt çekilir,
    yoksa sayfadaki Excel/CSV indirme linkini bulup oradan çeker.
    Bu fonksiyon sync çalışır; async fetch_source tarafından çağrılır.
    """
    records = []
    try:
        text = content.decode("utf-8", errors="replace")

        # CSV/Excel link ara (sayfada "Click for the Whole List" gibi bir link olabilir)
        import re
        csv_links = re.findall(r'href=["\']([^"\']*\.csv)["\']', text, re.IGNORECASE)
        excel_links = re.findall(r'href=["\']([^"\']*\.xlsx?)["\']', text, re.IGNORECASE)

        # Direkt tablo parse et (basit HTML tabloları için)
        # <table> içindeki <tr> satırlarını çek
        rows = re.findall(r'<tr[^>]*>(.*?)</tr>', text, re.DOTALL | re.IGNORECASE)
        for row in rows[1:]:  # İlk satır başlık
            cells = re.findall(r'<t[dh][^>]*>(.*?)</t[dh]>', row, re.DOTALL | re.IGNORECASE)
            # HTML tag'lerini temizle
            cells = [re.sub(r'<[^>]+>', '', c).strip() for c in cells]
            cells = [c for c in cells if c]

            if len(cells) < 2:
                continue

            name = cells[0]
            if not name or len(name) < 2:
                continue

            entity_type = "individual"
            if any(kw in name.upper() for kw in ["LTD", "A.Ş.", "INC", "LLC", "CORP", "ŞİRKET", "VAKIF", "DERNEK"]):
                entity_type = "entity"

            records.append({
                "source": source_key,
                "name": name,
                "name_lower": name.lower(),
                "entity_type": entity_type,
                "programs": "TERÖR FİNANSMANININ ÖNLENMESİ / 6415",
                "remarks": " | ".join(cells[1:3]) if len(cells) > 1 else "",
            })

        # Eğer tablo bulunamadıysa ve CSV link varsa logla
        if not records and csv_links:
            logger.info(f"[{source_key}] HTML'de tablo bulunamadı, CSV link var: {csv_links[0]}")

    except Exception as e:
        logger.error(f"[{source_key}] MASAK HTML parse hatası: {e}")

    return records


def parse_opensanctions_csv(content: bytes) -> list[dict]:
    """OpenSanctions simple CSV parse eder."""
    records = []
    text = content.decode("utf-8", errors="replace")
    reader = csv.DictReader(io.StringIO(text))

    for row in reader:
        name = row.get("name") or row.get("caption") or ""
        if not name:
            continue

        schema = row.get("schema") or "Unknown"
        if schema == "Person":
            etype = "individual"
        elif schema in ("Vessel", "Ship"):
            etype = "vessel"
        elif schema == "Aircraft":
            etype = "aircraft"
        else:
            etype = "entity"

        # OpenSanctions'da aliases JSON array olarak geliyor
        aliases_raw = row.get("aliases") or "[]"
        try:
            aliases = json.loads(aliases_raw) if aliases_raw.startswith("[") else [aliases_raw]
        except Exception:
            aliases = []

        countries_raw = row.get("countries") or "[]"
        try:
            countries = json.loads(countries_raw) if countries_raw.startswith("[") else []
        except Exception:
            countries = []

        records.append({
            "source": "OPENSANCTIONS",
            "entity_id": row.get("id") or "",
            "name": name,
            "name_lower": name.lower(),
            "entity_type": etype,
            "aliases": json.dumps(aliases, ensure_ascii=False),
            "countries": json.dumps(countries),
            "programs": row.get("topics") or "",
            "listed_on": row.get("first_seen") or "",
        })

    return records


# ── Veritabanına Yazma ────────────────────────────────────────────────────────

def upsert_records(records: list[dict], source_key: str):
    """Kayıtları veritabanına ekle/güncelle."""
    if not records:
        return 0

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    now = datetime.now(timezone.utc).isoformat()

    # Bu kaynağın mevcut kayıtlarını deaktif et
    c.execute("UPDATE sanctions SET is_active = 0 WHERE source = ?", (source_key,))

    inserted = 0
    for r in records:
        c.execute("""
            INSERT INTO sanctions
              (source, entity_id, name, name_lower, aliases, entity_type,
               programs, countries, dob, addresses, identifiers,
               listed_on, remarks, is_active, created_at, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,1,?,?)
        """, (
            r.get("source", source_key),
            r.get("entity_id"),
            r.get("name", ""),
            r.get("name_lower", ""),
            r.get("aliases"),
            r.get("entity_type"),
            r.get("programs"),
            r.get("countries"),
            r.get("dob"),
            r.get("addresses"),
            r.get("identifiers"),
            r.get("listed_on"),
            r.get("remarks"),
            now,
            now,
        ))
        inserted += 1

    conn.commit()
    conn.close()
    return inserted


def upsert_crypto(records: list[dict], source_key: str):
    """Kripto adreslerini veritabanına ekle."""
    if not records:
        return 0
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    now = datetime.now(timezone.utc).isoformat()

    c.execute("UPDATE crypto_sanctions SET is_active = 0 WHERE source = ?", (source_key,))

    inserted = 0
    for r in records:
        c.execute("""
            INSERT INTO crypto_sanctions
              (source, address, address_lower, blockchain, entity_name, program, listed_on, is_active)
            VALUES (?,?,?,?,?,?,?,1)
        """, (
            source_key,
            r.get("address", ""),
            r.get("address", "").lower(),
            r.get("blockchain"),
            r.get("entity_name"),
            r.get("program"),
            now,
        ))
        inserted += 1

    conn.commit()
    conn.close()
    return inserted


# ── OFAC Kripto Adresleri ────────────────────────────────────────────────────

OFAC_CRYPTO_URL = "https://www.treasury.gov/ofac/downloads/sanctions/1.0/sdn_advanced.xml"

def parse_ofac_crypto_xml(content: bytes) -> list[dict]:
    """OFAC SDN Advanced XML'den kripto adreslerini çıkart."""
    records = []
    try:
        root = ET.fromstring(content)
        ns = {"s": "https://sanctionslistservice.ofac.treas.gov/api/PublicationPreview/exports/ADVANCED_XML"}

        for entry in root.findall(".//s:sdnEntry", ns) or root.findall(".//sdnEntry"):
            # İsim
            last_name = ""
            first_name = ""
            for el in entry.iter():
                tag = el.tag.split("}")[-1]
                if tag == "lastName" and el.text:
                    last_name = el.text
                elif tag == "firstName" and el.text:
                    first_name = el.text

            entity_name = f"{first_name} {last_name}".strip() or "Unknown"

            # Kripto adresleri
            for id_el in entry.iter():
                tag = id_el.tag.split("}")[-1]
                if tag == "idType" and id_el.text and "Digital Currency" in id_el.text:
                    # Kardeş elementi bul
                    parent = list(entry.iter())
                    for i, el in enumerate(parent):
                        if el is id_el and i + 1 < len(parent):
                            addr_el = parent[i + 1]
                            addr_tag = addr_el.tag.split("}")[-1]
                            if addr_tag == "idNumber" and addr_el.text:
                                blockchain = "ETH" if addr_el.text.startswith("0x") else "BTC"
                                if id_el.text and "XBT" in id_el.text:
                                    blockchain = "BTC"
                                elif id_el.text and "ETH" in id_el.text:
                                    blockchain = "ETH"
                                records.append({
                                    "address": addr_el.text.strip(),
                                    "blockchain": blockchain,
                                    "entity_name": entity_name,
                                    "program": "OFAC-SDN",
                                })

    except Exception as e:
        logger.error(f"OFAC crypto XML parse hatası: {e}")

    return records


# ── Ana Fetch Fonksiyonu ──────────────────────────────────────────────────────

async def fetch_source(source_key: str, client: httpx.AsyncClient) -> tuple[int, str]:
    """Tek bir kaynağı indir ve veritabanına yaz. (kayıt sayısı, durum) döner."""
    cfg = SOURCES[source_key]
    t0 = asyncio.get_event_loop().time()

    logger.info(f"[{source_key}] İndiriliyor: {cfg['url']}")

    try:
        resp = await client.get(cfg["url"], timeout=60.0, follow_redirects=True)
        resp.raise_for_status()
        content = resp.content

        # ZIP ise aç
        if resp.url.path.endswith(".zip") or content[:2] == b"PK":
            with zipfile.ZipFile(io.BytesIO(content)) as z:
                # İlk CSV veya XML dosyayı al
                for name in z.namelist():
                    if name.endswith(".csv") or name.endswith(".xml"):
                        content = z.read(name)
                        break

        fmt = cfg["format"]
        if fmt == "csv":
            records = parse_ofac_csv(content, source_key)
        elif fmt == "xml_un":
            records = parse_un_xml(content)
        elif fmt == "xml_eu":
            records = parse_eu_xml(content)
        elif fmt == "csv_uk":
            records = parse_uk_csv(content)
        elif fmt == "csv_opensanctions":
            records = parse_opensanctions_csv(content)
        elif fmt in ("html_masak_domestic", "html_masak_foreign"):
            # MASAK: önce CSV indirme linkini bul, yoksa HTML parse et
            import re
            text = content.decode("utf-8", errors="replace")
            csv_match = re.search(
                r'href=["\']([^"\']*(?:7madde|fcib|domestic|yaptirim)[^"\']*\.csv)["\']',
                text, re.IGNORECASE
            )
            if csv_match:
                csv_url = csv_match.group(1)
                if not csv_url.startswith("http"):
                    csv_url = "https://en.hmb.gov.tr" + csv_url
                logger.info(f"[{source_key}] CSV link bulundu: {csv_url}")
                csv_resp = await client.get(csv_url, timeout=60.0, follow_redirects=True)
                if csv_resp.status_code == 200:
                    records = parse_masak_csv(csv_resp.content, source_key)
                else:
                    records = parse_masak_html(content, source_key)
            else:
                records = parse_masak_html(content, source_key)
        else:
            records = []

        count = upsert_records(records, source_key)
        duration = asyncio.get_event_loop().time() - t0

        # Log
        conn = sqlite3.connect(DB_PATH)
        conn.execute("""
            INSERT INTO fetch_log (source, fetched_at, record_count, status, duration_sec)
            VALUES (?,?,?,?,?)
        """, (source_key, datetime.now(timezone.utc).isoformat(), count, "success", duration))
        conn.commit()
        conn.close()

        logger.info(f"[{source_key}] ✓ {count} kayıt — {duration:.1f}s")
        return count, "success"

    except Exception as e:
        logger.error(f"[{source_key}] HATA: {e}")
        conn = sqlite3.connect(DB_PATH)
        conn.execute("""
            INSERT INTO fetch_log (source, fetched_at, record_count, status, error_msg)
            VALUES (?,?,0,?,?)
        """, (source_key, datetime.now(timezone.utc).isoformat(), "error", str(e)))
        conn.commit()
        conn.close()
        return 0, f"error: {e}"


async def fetch_all_sources():
    """Tüm kaynakları paralel indir."""
    init_db()
    logger.info("Tüm sanction listeleri güncelleniyor...")

    async with httpx.AsyncClient(
        headers={"User-Agent": "SanctionPay/1.0 compliance@sanctionpay.io"},
        timeout=90.0
    ) as client:
        tasks = [fetch_source(key, client) for key in SOURCES]
        results = await asyncio.gather(*tasks, return_exceptions=True)

    # OFAC kripto adresleri ayrıca
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.get(OFAC_CRYPTO_URL)
            if resp.status_code == 200:
                crypto_records = parse_ofac_crypto_xml(resp.content)
                count = upsert_crypto(crypto_records, "OFAC-SDN")
                logger.info(f"[OFAC-CRYPTO] ✓ {count} kripto adres")
    except Exception as e:
        logger.warning(f"[OFAC-CRYPTO] İndirilemedi: {e}")

    total = sum(r[0] for r in results if isinstance(r, tuple))
    logger.info(f"Güncelleme tamamlandı. Toplam: {total} kayıt")
    return results


# ── Arama Fonksiyonu ──────────────────────────────────────────────────────────

def search_name(query: str, threshold: float = 0.75) -> list[dict]:
    """
    İsim tabanlı fuzzy arama.
    Hem substring hem de token overlap kontrolü yapar.
    """
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    query_lower = query.lower().strip()
    query_tokens = set(query_lower.split())

    # Exact veya substring eşleşme
    c.execute("""
        SELECT * FROM sanctions
        WHERE is_active = 1 AND (
            name_lower LIKE ? OR
            aliases LIKE ?
        )
        ORDER BY name_lower
        LIMIT 50
    """, (f"%{query_lower}%", f"%{query_lower}%"))

    rows = [dict(r) for r in c.fetchall()]

    # Token overlap skoru ekle
    results = []
    for row in rows:
        name_tokens = set(row["name_lower"].split())
        overlap = len(query_tokens & name_tokens) / max(len(query_tokens), 1)
        row["match_score"] = overlap
        results.append(row)

    conn.close()
    return sorted(results, key=lambda x: x["match_score"], reverse=True)


def search_crypto_address(address: str) -> list[dict]:
    """Kripto adres araması."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    c.execute("""
        SELECT * FROM crypto_sanctions
        WHERE is_active = 1 AND address_lower = ?
    """, (address.lower(),))

    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return rows


def get_stats() -> dict:
    """Veritabanı istatistikleri."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    stats = {"sources": {}, "total": 0, "crypto_total": 0}

    c.execute("SELECT source, COUNT(*) as cnt FROM sanctions WHERE is_active=1 GROUP BY source")
    for row in c.fetchall():
        stats["sources"][row[0]] = row[1]
        stats["total"] += row[1]

    c.execute("SELECT COUNT(*) FROM crypto_sanctions WHERE is_active=1")
    stats["crypto_total"] = c.fetchone()[0]

    c.execute("SELECT source, fetched_at, record_count, status FROM fetch_log ORDER BY fetched_at DESC LIMIT 10")
    stats["recent_fetches"] = [dict(zip(["source","fetched_at","count","status"], r)) for r in c.fetchall()]

    conn.close()
    return stats


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    asyncio.run(fetch_all_sources())
