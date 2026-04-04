import asyncio
from playwright.async_api import async_playwright

async def test_pw():
    url = "https://maps.app.goo.gl/K1YNXaMc4fjsox8Q8"
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(locale="ja-JP")
        page = await context.new_page()
        
        # goto with wait_until='networkidle' or 'load'
        print("Starting goto...")
        await page.goto(url, wait_until="networkidle", timeout=30000)
        print("Page URL after goto:", page.url)
        
        # Sometimes there's a consent page in some regions, though locale="ja-JP" usually bypasses EU consent.
        # Check actual meta tags
        title = await page.evaluate("() => { const meta = document.querySelector('meta[property=\"og:title\"]'); return meta ? meta.content : document.title; }")
        desc = await page.evaluate("() => { const meta = document.querySelector('meta[property=\"og:description\"]'); return meta ? meta.content : ''; }")
        
        print("Title:", title)
        print("Desc:", desc)
        
        await browser.close()

if __name__ == "__main__":
    asyncio.run(test_pw())
