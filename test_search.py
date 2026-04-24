import asyncio
from playwright.async_api import async_playwright

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        res = await page.goto('https://html.duckduckgo.com/html/?q=test')
        print('DDG HTML:', res.status if res else 'None', await page.title())
        content = await page.content()
        print('DDG HTML Length:', len(content))
        if 'bot' in content.lower():
            print('DDG HTML: detected bot')
        
        res2 = await page.goto('https://www.bing.com/search?q=test')
        print('Bing:', res2.status if res2 else 'None', await page.title())
        content2 = await page.content()
        if 'bot' in content2.lower():
            print('Bing: detected bot')
            
        await browser.close()

if __name__ == '__main__':
    asyncio.run(main())
