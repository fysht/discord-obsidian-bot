import asyncio
import aiohttp
from bs4 import BeautifulSoup

async def get_with_aiohttp():
    url = "https://maps.app.goo.gl/K1YNXaMc4fjsox8Q8"
    async with aiohttp.ClientSession() as session:
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        async with session.get(url, headers=headers) as response:
            print("Final URL:", response.url)
            html = await response.text()
            soup = BeautifulSoup(html, 'html.parser')
            title = soup.find("meta", property="og:title")
            desc = soup.find("meta", property="og:description")
            print("Title:", title["content"] if title else soup.title.string)
            print("Desc:", desc["content"] if desc else "None")

if __name__ == "__main__":
    asyncio.run(get_with_aiohttp())
