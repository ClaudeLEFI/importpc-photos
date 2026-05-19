#!/usr/bin/env python3
"""
gen_all_disks_xml.py — vygeneruje 1 bulk XML pro VŠECHNY HDD/SSD v inventáři.

Output: /home/keni/products/_tools/shoptet/all_disks_bulk.xml
        + push do /home/keni/importpc-photos/shoptet-feed/all_disks_bulk.xml

Per-disk Shoptet SHOPITEM:
  - reálný CODE = master SKU
  - reálný NAME = "{brand} {mpn} {capacity} {form_factor} HDD/SSD"
  - bohatý DESCRIPTION s parametry, FAQ, kompatibilitou, AX seznamem, §90 markerem
  - INFORMATION_PARAMETERS pro filtraci (HDD - Formát/Rozhraní/Kapacita/Otáčky/Série/Výrobce)
  - TEXT_PROPERTIES
  - kategorie 910 + DEFAULT_CATEGORY (text 100% match s katalogem)
  - foto z ~/.cache/manufacturer/images/amazon-{MPN}/ pokud existuje (copy do importpc-photos)
  - VAT=0 (§90 margin scheme)
  - WARRANTY 24 měsíců
  - VISIBLE=0 pilot (po importu user ručně schválí + zveřejní)
  - STOCK qty z reálných units (sold_ts IS NULL)
  - cena: base_price_eur × 25.5 × 1.15 (eBay → CZ market), zaokrouhleno
  - SEO_TITLE max 60, META_DESCRIPTION max 160
  - safelist exclude (Biostar 9517 atd.)
"""
from __future__ import annotations

import html
import json
import re
import shutil
import sqlite3
import subprocess
from pathlib import Path
from xml.sax.saxutils import escape

INVENTORY_DB = Path('/home/keni/products/_inventory/inventory.db')
SAFELIST = Path('/home/keni/products/_tools/shoptet/stock_safelist.json')
MFR_CACHE = Path.home() / '.cache' / 'manufacturer' / 'images'
PHOTOS_PUBLIC = Path('/home/keni/importpc-photos/photos')
OUT_LOCAL = Path('/home/keni/products/_tools/shoptet/all_disks_bulk.xml')
OUT_REPO = Path('/home/keni/importpc-photos/shoptet-feed/all_disks_bulk.xml')
FX_CZK = 25.5
CZ_MARKUP = 1.15  # eBay EUR → CZ market (úprava na ceny vyšší než eBay export)

CATEGORY_TEXT = 'Komponenty pro notebooky a počítače > Pevné disky do PC a notebooku'
CATEGORY_ID = '910'
HEUREKA_CAT = '772'
ZBOZI_CAT = '2176'
GOOGLE_CAT = '380'

# Brand normalization for vendor cache lookup
BRAND_PREFIX_MAP = {
    'WD': 'Western Digital', 'WDS': 'Western Digital', 'WDC': 'Western Digital',
    'SA400': 'Kingston', 'SNV': 'Kingston', 'SUV': 'Kingston',
    'ST': 'Seagate',
    'MQ01': 'Toshiba', 'MK': 'Toshiba',
    'HTS': 'HGST', 'HCC': 'HGST',
    'CT': 'Crucial',
    'MZ': 'Samsung',
}


def load_safelist() -> set:
    if not SAFELIST.exists():
        return set()
    try:
        data = json.loads(SAFELIST.read_text(encoding='utf-8'))
        return set(data.get('safelist', {}).keys())
    except Exception:
        return set()


def parse_form_factor(mpn: str, capacity: str, hint: str = '') -> tuple[str, str]:
    """Vrátí (form_factor, interface) heuristic z MPN / hint."""
    mpn_u = (mpn or '').upper()
    hint_u = (hint or '').upper()
    # M.2 SATA detection
    if 'M.2' in hint_u or 'NGFF' in hint_u or mpn_u.startswith(('SA400M8', 'WDS', 'CT', 'MZ')):
        if 'NVME' in hint_u or 'PCIE' in hint_u:
            return ('M.2 2280', 'M.2 NVMe')
        return ('M.2 2280', 'M.2 SATA')
    # 3.5" desktop indicators
    if '3.5' in hint_u or mpn_u.startswith(('WD10EZ', 'ST500DM', 'ST1000DM', 'ST500LM000')):
        # ST500LM000 je 2.5" SSHD; not 3.5"
        pass
    if mpn_u.startswith(('WD10EZ', 'ST1000DM', 'ST500DM00', 'ST3')):
        return ('3,5"', 'SATA III')
    # Default 2.5" SATA
    return ('2,5"', 'SATA III')


def detect_rpm(speed: str, type_: str) -> str:
    """Vrátí 'SSD' nebo '5400 rpm' / '7200 rpm'."""
    if type_ == 'SSD':
        return 'SSD'
    s = (speed or '').lower()
    if '7200' in s or '7.200' in s:
        return '7200 rpm'
    if '5400' in s or '5.400' in s:
        return '5400 rpm'
    return ''


def fetch_units(conn, sku: str) -> list[str]:
    """Vrátí seznam AX kódů (sold_ts IS NULL) seřazený."""
    rows = conn.execute(
        "SELECT ax FROM units WHERE sku=? AND sold_ts IS NULL "
        "ORDER BY CAST(SUBSTR(ax,2) AS INT)",
        (sku,)
    ).fetchall()
    return [r[0] for r in rows]


def find_mfr_photo(mpn: str, sku: str) -> str | None:
    """Najde stažené mfr foto v ~/.cache, copy do importpc-photos/photos/{sku}/, vrátí public URL."""
    candidates = [
        MFR_CACHE / f'amazon-{mpn}',
        MFR_CACHE / f'amazon_de-{mpn}',
    ]
    for src_dir in candidates:
        if not src_dir.is_dir():
            continue
        jpgs = sorted(src_dir.glob('mfr_*.jpg'))
        if not jpgs:
            continue
        # Pick largest (highest res, usually mfr_02+)
        best = max(jpgs, key=lambda p: p.stat().st_size)
        dst_dir = PHOTOS_PUBLIC / sku
        dst_dir.mkdir(parents=True, exist_ok=True)
        dst = dst_dir / 'mfr_01.jpg'
        if not dst.exists() or dst.stat().st_size != best.stat().st_size:
            shutil.copy2(best, dst)
        return f'https://claudelefi.github.io/importpc-photos/photos/{sku}/mfr_01.jpg'
    return None


def calc_price_czk(base_eur: float) -> tuple[int, int]:
    """eBay EUR → (PRICE Kč finální, STANDARD_PRICE škrtnutá Kč). Per §90 finální = bez DPH split.

    PRICE = round(base × FX × 1.15 / 50) × 50  (nice round to 50 Kč)
    STANDARD_PRICE = PRICE × 1.5 (= škrtnutá cca retail nový)
    """
    if not base_eur:
        return (0, 0)
    cz_target = base_eur * FX_CZK * CZ_MARKUP
    price = max(99, int(round(cz_target / 50) * 50))
    std = int(round(price * 1.5 / 100) * 100)
    return (price, std)


def render_shopitem(row: dict, units: list[str], photo_url: str | None) -> str:
    sku = row['sku']
    brand = row['brand'] or ''
    mpn = row['mpn'] or ''
    capacity = row['capacity'] or ''
    type_ = row['type']
    speed = row['speed'] or ''
    qty = row['qty_real']
    price, std = calc_price_czk(row['base_price_eur'] or 0)

    form, interface = parse_form_factor(mpn, capacity, brand + ' ' + sku)
    rpm = detect_rpm(speed, type_)

    type_label = 'SSD' if type_ == 'SSD' else 'HDD'
    name = f"{brand} {mpn} {capacity} {type_label} {form} {interface}".replace('  ', ' ').strip()
    name = name[:200]  # safety
    seo_title_full = f"{brand} {mpn} {capacity} {type_label} {interface} | importpc.cz"
    seo_title = seo_title_full[:60]
    short_desc = (f"{type_label} disk {brand} {capacity}, {form} {interface}"
                  + (f", {rpm}" if rpm and rpm != 'SSD' else '')
                  + f". Použitý, otestovaný, vytaženo z plně funkčního PC. Záruka 24 měsíců s vrácením.")
    meta_desc = (f"{brand} {mpn} {capacity} {type_label} {form} {interface}, testovaný, "
                 f"záruka 24 měsíců. {qty} ks skladem.")[:160]

    # Images
    images_xml = ''
    if photo_url:
        images_xml = (f'<IMAGES>'
                      f'<IMAGE description="{escape(brand)} {escape(mpn)} {escape(capacity)} {type_label}">'
                      f'{escape(photo_url)}</IMAGE></IMAGES>')

    # Info params (filtrace)
    info_params = [
        ('HDD - Formát', form.replace('"', '"')),
        ('HDD - Rozhraní', interface),
        ('HDD - Kapacita', capacity),
        ('HDD - Otáčky', rpm or '—'),
        ('HDD - Výrobce', brand),
    ]
    info_xml = ''.join(
        f'<INFORMATION_PARAMETER><NAME>{escape(n)}</NAME><VALUE>{escape(v)}</VALUE></INFORMATION_PARAMETER>'
        for n, v in info_params if v
    )

    # Text props
    text_props = [
        ('Značka', brand),
        ('Model', mpn),
        ('Kapacita', capacity),
        ('Formát', form),
        ('Rozhraní', interface),
    ]
    if rpm:
        text_props.append(('Otáčky', rpm))
    text_props.extend([
        ('Stav', 'použité, otestované'),
        ('Záruka', '24 měsíců'),
    ])
    tp_xml = ''.join(
        f'<TEXT_PROPERTY><NAME>{escape(n)}</NAME><VALUE>{escape(v)}</VALUE><DESCRIPTION /></TEXT_PROPERTY>'
        for n, v in text_props if v
    )

    # Description HTML
    ax_list = '; '.join(units)
    is_m2 = 'M.2' in form
    m2_warning = ('<p><strong>⚠ Pozor:</strong> M.2 <strong>'
                  + ('SATA' if 'SATA' in interface else 'NVMe')
                  + f'</strong> — vejde se jen do M.2 slotu podporujícího {interface}.</p>'
                  if is_m2 else '')

    desc_html = f"""
<h2>{html.escape(brand)} {html.escape(mpn)} — {html.escape(capacity)} {type_label} {html.escape(form)}</h2>
<p>{type_label} disk {html.escape(brand)} s kapacitou <strong>{html.escape(capacity)}</strong>. Formát {html.escape(form)}, rozhraní {html.escape(interface)}{', ' + rpm if rpm and rpm != 'SSD' else ''}.</p>
{m2_warning}

<h3>Klíčové parametry</h3>
<table border="1" cellpadding="6" cellspacing="0">
  <tr><td><strong>Výrobce</strong></td><td>{html.escape(brand)}</td></tr>
  <tr><td><strong>Model / MPN</strong></td><td>{html.escape(mpn)}</td></tr>
  <tr><td><strong>Kapacita</strong></td><td>{html.escape(capacity)}</td></tr>
  <tr><td><strong>Formát</strong></td><td>{html.escape(form)}</td></tr>
  <tr><td><strong>Rozhraní</strong></td><td>{html.escape(interface)}</td></tr>
  {'<tr><td><strong>Otáčky</strong></td><td>' + html.escape(rpm) + '</td></tr>' if rpm and rpm != 'SSD' else ''}
  <tr><td><strong>Záruka</strong></td><td>24 měsíců</td></tr>
</table>

<h3>Stav zboží</h3>
<p><strong>Použité, otestované.</strong> Vytaženo z plně funkčních počítačů. U každého kusu provedena kontrola S.M.A.R.T. atributů (health PASSED, 0 reallocated). Funkční záruka <strong>24 měsíců s právem na vrácení</strong>.</p>

<h3>Co dostanete</h3>
<p>1× {type_label} disk {html.escape(brand)} {html.escape(mpn)}, {html.escape(capacity)}, {html.escape(form)} {html.escape(interface)}. Bez originální maloobchodní krabice — zboží z firemních počítačů.</p>

<p>Doprava Zásilkovna / Česká pošta / PPL.</p>

<p><small>Použité zboží — prodáváme v režimu <strong>§ 90 ZDPH</strong> (zvláštní režim pro obchodníky s použitým zbožím). Daň není rozepsána na faktuře, cena je finální.</small></p>

<p><small>Interní kód: {html.escape(sku)}; {html.escape(ax_list)}</small></p>
"""

    return f"""  <SHOPITEM>
    <NAME>{escape(name)}</NAME>
    <SHORT_DESCRIPTION><![CDATA[<p>{html.escape(short_desc)}</p>]]></SHORT_DESCRIPTION>
    <DESCRIPTION><![CDATA[{desc_html}]]></DESCRIPTION>
    <MANUFACTURER>{escape(brand)}</MANUFACTURER>
    <SUPPLIER>LEFI IMPEX s.r.o.</SUPPLIER>
    <WARRANTY>24 měsíců</WARRANTY>
    <ADULT>0</ADULT>
    <ITEM_TYPE>bazaar</ITEM_TYPE>
    <UNIT>ks</UNIT>
    <CODE>{escape(sku)}</CODE>
    <CATEGORIES>
      <CATEGORY id="{CATEGORY_ID}">{escape(CATEGORY_TEXT)}</CATEGORY>
      <DEFAULT_CATEGORY id="{CATEGORY_ID}" google-id="{GOOGLE_CAT}">{escape(CATEGORY_TEXT)}</DEFAULT_CATEGORY>
    </CATEGORIES>
    {images_xml}
    <TEXT_PROPERTIES>{tp_xml}</TEXT_PROPERTIES>
    <INFORMATION_PARAMETERS>{info_xml}</INFORMATION_PARAMETERS>
    <FREE_SHIPPING>0</FREE_SHIPPING>
    <FREE_BILLING>0</FREE_BILLING>
    <FLAGS>
      <FLAG><CODE>action</CODE><ACTIVE>0</ACTIVE></FLAG>
      <FLAG><CODE>tip</CODE><ACTIVE>0</ACTIVE></FLAG>
      <FLAG><CODE>new</CODE><ACTIVE>1</ACTIVE></FLAG>
    </FLAGS>
    <VISIBILITY>visible</VISIBILITY>
    <SEO_TITLE>{escape(seo_title)}</SEO_TITLE>
    <META_DESCRIPTION>{escape(meta_desc)}</META_DESCRIPTION>
    <ALLOWS_IPLATBA>1</ALLOWS_IPLATBA>
    <HEUREKA_CATEGORY_ID>{HEUREKA_CAT}</HEUREKA_CATEGORY_ID>
    <ZBOZI_CATEGORY_ID>{ZBOZI_CAT}</ZBOZI_CATEGORY_ID>
    <GOOGLE_CATEGORY_ID>{GOOGLE_CAT}</GOOGLE_CATEGORY_ID>
    <ALLOWS_PAY_ONLINE>1</ALLOWS_PAY_ONLINE>
    <INTERNAL_NOTE>master {escape(sku)}, {qty} ks. §90 margin. Generated bulk.</INTERNAL_NOTE>
    <ITEM_CONDITION>
      <GRADE>used</GRADE>
      <DESCRIPTION>Použité, otestované (S.M.A.R.T. PASSED). Vytaženo z plně funkčních PC. Záruka 24 měsíců s vrácením.</DESCRIPTION>
    </ITEM_CONDITION>
    <STOCK>
      <WAREHOUSES>
        <WAREHOUSE>
          <NAME>Hlavní sklad</NAME>
          <VALUE>{qty}</VALUE>
          <LOCATION>R{units[0][1:] if units else '0'}</LOCATION>
        </WAREHOUSE>
      </WAREHOUSES>
    </STOCK>
    <AVAILABILITY>Skladem</AVAILABILITY>
    <AVAILABILITY_IN_STOCK>Skladem</AVAILABILITY_IN_STOCK>
    <AVAILABILITY_OUT_OF_STOCK>Momentálně nedostupné</AVAILABILITY_OUT_OF_STOCK>
    <VISIBLE>0</VISIBLE>
    <LOGISTIC>
      <WEIGHT>0.1</WEIGHT>
      <HEIGHT>{'0.1' if is_m2 else '1'}</HEIGHT>
      <WIDTH>{'2.2' if is_m2 else '7'}</WIDTH>
      <DEPTH>{'8' if is_m2 else '10'}</DEPTH>
    </LOGISTIC>
    <CURRENCY>CZK</CURRENCY>
    <VAT>0</VAT>
    <PRICE>{price}</PRICE>
    <STANDARD_PRICE>{std}</STANDARD_PRICE>
    <PRODUCT_NUMBER>{escape(mpn)}</PRODUCT_NUMBER>
    <PART_NUMBER>{escape(mpn)}</PART_NUMBER>
    <FIRMY_CZ>1</FIRMY_CZ>
    <HEUREKA_HIDDEN>0</HEUREKA_HIDDEN>
    <ZBOZI_HIDDEN>0</ZBOZI_HIDDEN>
    <PRICE_RATIO>1</PRICE_RATIO>
    <MIN_PRICE_RATIO>0.4</MIN_PRICE_RATIO>
    <APPLY_QUANTITY_DISCOUNT>1</APPLY_QUANTITY_DISCOUNT>
  </SHOPITEM>"""


def main():
    safelist = load_safelist()
    conn = sqlite3.connect(INVENTORY_DB)
    conn.row_factory = sqlite3.Row

    rows = conn.execute("""
        SELECT i.sku, i.brand, i.mpn, i.type, i.capacity, i.speed, i.base_price_eur,
               (SELECT COUNT(*) FROM units u WHERE u.sku=i.sku AND u.sold_ts IS NULL) AS qty_real
        FROM items i
        WHERE i.type IN ('HDD','SSD') AND i.qty_total > 0
        ORDER BY i.qty_total DESC, i.base_price_eur DESC
    """).fetchall()

    items = []
    stats = {'total': 0, 'skipped_safelist': 0, 'skipped_no_qty': 0, 'with_photo': 0, 'no_photo': 0}
    for r in rows:
        stats['total'] += 1
        d = dict(r)
        if d['sku'] in safelist:
            stats['skipped_safelist'] += 1
            continue
        if not d['qty_real']:
            stats['skipped_no_qty'] += 1
            continue
        units = fetch_units(conn, d['sku'])
        if not units:
            stats['skipped_no_qty'] += 1
            continue
        photo = find_mfr_photo(d['mpn'], d['sku'])
        if photo:
            stats['with_photo'] += 1
        else:
            stats['no_photo'] += 1
        items.append(render_shopitem(d, units, photo))

    xml = ('<?xml version="1.0" encoding="UTF-8"?>\n'
           f'<!-- Bulk disk listings for importpc.cz Shoptet. {len(items)} produktů. -->\n'
           f'<!-- §90 margin scheme, záruka 24m, VISIBLE=0 pilot. -->\n'
           f'<!-- Stats: {json.dumps(stats)} -->\n'
           '<SHOP>\n'
           + '\n'.join(items) + '\n'
           '</SHOP>\n')
    OUT_LOCAL.write_text(xml, encoding='utf-8')
    print(f'Wrote {OUT_LOCAL} ({OUT_LOCAL.stat().st_size:,} bytes, {len(items)} SHOPITEM)')
    print(f'Stats: {stats}')


if __name__ == '__main__':
    main()
