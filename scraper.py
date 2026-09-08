import re
import urllib.parse
import asyncio
import nodriver as uc
from bs4 import BeautifulSoup
import os
import json
import sys
import mysql.connector

def get_db_connection():
    return mysql.connector.connect(
        host=os.getenv("DB_HOST", ""),
        user=os.getenv("DB_USER", "harish_rating_tool"),
        password=os.getenv("DB_PASSWORD", "gravyLolipop"),
        database=os.getenv("DB_NAME", "harish_rating_tool"),
        port=int(os.getenv("DB_PORT", 3306))
    )


# Force UTF-8 for console output to avoid Windows charmap errors with emojis
if sys.platform == 'win32':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except:
        pass

async def scrape_google_search(keyword: str):
    """
    Scrapes Google Search directly for the Knowledge Panel.
    Extracts Google ratings and 'Reviews from the web' (Zomato, Magicpin, Justdial).
    Returns a dictionary of platform ratings.
    """
    results = {}
    
    browser = None
    try:
        # Start browser headful to pass bot checks
        browser = await uc.start(
            headless=False,
            browser_args=[
                '--disable-blink-features=AutomationControlled',
                '--disable-infobars',
                '--window-size=1280,720',
                '--window-position=-32000,-32000' # Hide window off-screen
            ]
        )
        
        # Open Google
        encoded_keyword = urllib.parse.quote(keyword)
        search_url = f"https://www.google.com/search?q={encoded_keyword}"
        
        page = await browser.get(search_url)
        
        # Wait for potential Captcha or slow loading
        for _ in range(10):
            content = await page.get_content()
            if 'id="search"' in content or 'id="rhs"' in content or 'id="rcnt"' in content:
                break
            await asyncio.sleep(1)
            
        await asyncio.sleep(2)
        
        html = await page.get_content()
        soup = BeautifulSoup(html, 'html.parser')
        
        # Fallback text matching since Google classes change constantly
        page_text = soup.get_text(separator=' ', strip=True).lower()
        
        # 1. Parse Google Native Reviews from the top of the Knowledge panel
        # Looking for things like "4.5 (1,234) · Google reviews" or similar
        google_pattern = r'(\d[\.\,]\d|\d)\s*(?:\()([\d\,]+)(?:\)).{0,30}?(?:google|review)'
        g_match = re.search(google_pattern, page_text)
        if g_match:
            try:
                g_rating = float(g_match.group(1).replace(',', '.'))
                g_count = int(g_match.group(2).replace(',', ''))
                results['google'] = {'rating': g_rating, 'count': g_count}
            except: pass
            
        # 2. Parse "Reviews from the web" section for external platforms
        platforms_to_check = ['justdial', 'magicpin', 'zomato']
        
        for p in platforms_to_check:
            # Regex looks for: platform name, followed by up to 50 chars, followed by rating, followed by votes
            pattern = rf'{p}.{{0,50}}?(\d[\.\,]\d|\d)\s*(?:/|out of)?\s*5?.{{0,30}}?([\d\,]+)\s*(?:votes|reviews|ratings)'
            match = re.search(pattern, page_text)
            
            if match:
                try:
                    rating = float(match.group(1).replace(',', '.'))
                    count = int(match.group(2).replace(',', ''))
                    
                    if rating > 0 or count > 0:
                        results[p] = {'rating': rating, 'count': count}
                except Exception as e:
                    print(f"Regex match failed parsing for {p}: {e}")
                    
        return results
        
    except Exception as e:
        import traceback
        print(f"Scraper Exception: {e}")
        traceback.print_exc()
        return results
    finally:
        if browser:
            browser.stop()

async def scrape_google_maps(keyword: str, category: str, industry: str, actual_keyword: str = None, batch_name: str = None):
    """
    Requested to scrape from Google Search instead of Google Maps.
    We will use Google Search and store into businesses and external_reviews.
    """
    print(f"Scraping {keyword} using Google Search (Chrome)...")
    
    # Use actual_keyword if provided, else keyword
    search_query = actual_keyword if actual_keyword else keyword
    
    # We can reuse scrape_google_search for fetching
    # But wait, scrape_google_search returns dict, we want to extract brand name and save to DB
    # Let's run scrape_google_search and then write the results to the database!
    
    results = await scrape_google_search(search_query)
    
    # Try to extract brand_name, rating, count
    # Since scrape_google_search doesn't return brand_name, address, phone, we'll use placeholder or what we know
    brand_name = search_query
    maps_url = ""
    address = ""
    phone = ""
    
    google_rating = 0.0
    google_review_count = 0
    if 'google' in results:
        google_rating = results['google']['rating']
        google_review_count = results['google']['count']
        
    db = get_db_connection()
    cursor = db.cursor()
    
    try:
        # Check if the businesses table has batch_name
        # The schema we checked earlier had batch_name!
        insert_bus = """
        INSERT INTO businesses (brand_name, google_maps_url, category, industry, address, phone, google_rating, google_review_count, batch_name)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        """
        cursor.execute(insert_bus, (brand_name, maps_url, category, industry, address, phone, google_rating, google_review_count, batch_name))
        business_id = cursor.lastrowid
        
        platforms = ['justdial', 'indiamart', 'magicpin', 'tripadvisor', 'zomato', 'swiggy', 'facebook', 'dineout', 'eazydiner']
        
        for p in platforms:
            if p in results:
                insert_rev = """
                INSERT INTO external_reviews (business_id, platform_name, platform_url, rating, review_count)
                VALUES (%s, %s, %s, %s, %s)
                """
                cursor.execute(insert_rev, (business_id, p, "", results[p]['rating'], results[p]['count']))
                
        db.commit()
        print(f"Scrape completed for {keyword} and saved to DB.")
    except Exception as e:
        import traceback
        print(f"Error saving to DB for {keyword}: {e}")
        traceback.print_exc()
    finally:
        cursor.close()
        db.close()
    return results
