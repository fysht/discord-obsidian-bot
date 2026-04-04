import asyncio
from playwright.async_api import async_playwright

async def test_pw():
    url = "https://maps.app.goo.gl/K1YNXaMc4fjsox8Q8"
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(locale="ja-JP")
        page = await context.new_page()
        
        await page.goto(url, wait_until="domcontentloaded")
        await asyncio.sleep(3) # wait a bit for JS
        
        body_text = await page.evaluate("() => document.body.innerText")
        print("Body Text excerpt:", body_text[:500])
        
        h1s = await page.evaluate("() => Array.from(document.querySelectorAll('h1')).map(e => e.innerText)")
        print("H1s:", h1s)
        
        await browser.close()

if __name__ == "__main__":
    asyncio.run(test_pw())
