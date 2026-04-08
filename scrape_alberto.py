"""
Scraper de tweets de @alferdez via Wayback Machine.
Ejecutar con: python3 scrape_alberto.py
Genera/actualiza alberto_tweets.json con tweets reales.
"""

import urllib.request, urllib.error
import json, re, time, random, os
from concurrent.futures import ThreadPoolExecutor, as_completed

OUTPUT_FILE = "alberto_tweets.json"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"

def cdx_get_ids(year, limit=500):
    url = (
        f"https://web.archive.org/cdx/search/cdx"
        f"?url=twitter.com/alferdez/status/*"
        f"&output=json&fl=original,timestamp"
        f"&from={year}0101&to={year}1231"
        f"&filter=statuscode:200&collapse=urlkey&limit={limit}"
    )
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.loads(r.read())
        seen, result = set(), []
        for row in data[1:]:
            m = re.search(r"/status/(\d+)", row[0])
            if m and m.group(1) not in seen:
                seen.add(m.group(1))
                result.append((m.group(1), row[0], row[1]))
        return result
    except Exception as e:
        print(f"  CDX {year}: {e}")
        return []

def fetch_tweet_text(tid, orig_url, ts):
    archive_url = f"https://web.archive.org/web/{ts}/{orig_url}"
    try:
        req = urllib.request.Request(archive_url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=12) as r:
            html = r.read().decode("utf-8", errors="ignore")
        # og:description contiene el texto del tweet
        m = re.search(r'og:description[^>]+content="([^"]+)"', html)
        if m:
            text = m.group(1).strip()
            if text and len(text) > 10 and "twitter.com" not in text[:30]:
                return tid, text
    except Exception:
        pass
    return tid, None

def main():
    # Cargar tweets existentes
    existing = {}
    if os.path.exists(OUTPUT_FILE):
        with open(OUTPUT_FILE) as f:
            existing = json.load(f)
    print(f"Tweets existentes: {len(existing)}")

    for year in range(2019, 2024):
        print(f"\nAño {year}: obteniendo IDs...")
        ids = cdx_get_ids(year, limit=600)
        new_ids = [(tid, url, ts) for tid, url, ts in ids if tid not in existing]
        print(f"  {len(ids)} únicos, {len(new_ids)} nuevos por fetchear")

        fetched = 0
        with ThreadPoolExecutor(max_workers=8) as ex:
            futures = {ex.submit(fetch_tweet_text, tid, url, ts): tid
                       for tid, url, ts in new_ids}
            for fut in as_completed(futures):
                tid, text = fut.result()
                if text:
                    existing[tid] = text
                    fetched += 1
                    if fetched % 20 == 0:
                        print(f"    {fetched} tweets obtenidos este año...")
        print(f"  Año {year}: {fetched} tweets nuevos. Total: {len(existing)}")
        # Guardar después de cada año
        with open(OUTPUT_FILE, "w") as f:
            json.dump(existing, f, ensure_ascii=False, indent=2)
        time.sleep(2)

    print(f"\nFinalizado. Total de tweets: {len(existing)}")
    print(f"Guardado en {OUTPUT_FILE}")

if __name__ == "__main__":
    main()
