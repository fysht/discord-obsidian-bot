import asyncio
from playwright.async_api import async_playwright

async def test_pw():
    url = "https://maps.app.goo.gl/K1YNXaMc4fjsox8Q8"
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(locale="ja-JP")
        page = await context.new_page()
        
        print("Starting goto...")
        await page.goto(url, wait_until="domcontentloaded")
        print("DOM loaded. URL:", page.url)
        
        try:
            # Wait for the og:title to be updated to something other than 'Google Maps'
            await page.wait_for_function(
                "() => { const m = document.querySelector('meta[property=\"og:title\"]'); return m && m.content && m.content !== 'Google Maps'; }",
                timeout=10000
            )
            print("Meta tag updated!")
        except Exception as e:
            print("Timeout waiting for meta tag:", e)
            
        title = await page.evaluate("() => { const meta = document.querySelector('meta[property=\"og:title\"]'); return meta ? meta.content : document.title; }")
        desc = await page.evaluate("() => { const meta = document.querySelector('meta[property=\"og:description\"]'); return meta ? meta.content : ''; }")
        
        print("Title:", title)
        print("Desc:", desc)
        
        await browser.close()

if __name__ == "__main__":
    asyncio.run(test_pw())
