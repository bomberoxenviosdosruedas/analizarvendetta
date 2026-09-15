"""
Script de automatización de navegador con Playwright para Vendetta Legacy.
Inicia sesión emulando un navegador real, resuelve el formulario de login (servidor s1, name y password),
navega por las diferentes subpáginas del juego y guarda el HTML completamente renderizado.

Credenciales:
Usuario: Bomberox
Servidor: s1
"""

import asyncio
import json
import logging
import os
import re
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Set, Dict, Optional
from playwright.async_api import async_playwright, Browser, Page, TimeoutError as PlaywrightTimeout

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S"
)
logger = logging.getLogger("VendettaPlaywright")


@dataclass
class PlaywrightScraperConfig:
    base_url: str = "https://vendettalegacy.es"
    username: str = "Bomberox"
    password: str = "25Qkg4PrHZwVXt4"
    server: str = "s1"
    output_dir: Path = Path("vendetta_playwright_html")
    headless: bool = True
    slow_mo_ms: int = 50
    viewport: dict = field(default_factory=lambda: {"width": 1440, "height": 900})
    # Rutas iniciales a recorrer
    core_pages: List[str] = field(default_factory=lambda: [
        "overview",
        "overview/buildings",
        "overview/attack-power",
        "overview/global-vision",
        "resources",
        "resources/recursos",
        "facilities",
        "shipyard",
        "shipyard/masivo",
        "defense",
        "fleet",
        "fleet/movement",
        "techtree?type=hab",
        "techtree?type=ent",
        "techtree?type=tr",
        "mapa",
        "alliance",
        "battle-sim",
        "granjas",
        "merchant",
        "padrino",
        "messages",
        "messages?tab=fleets",
        "messages?tab=communication&subtab=messages",
        "chat",
        "buddies",
        "records",
        "rules",
        "highscore",
        "highscore?category=2",
        "highscore/servers",
        "salon-de-la-fama",
        "options",
        "suggestions",
        "characterclass",
        "overlay/notes",
        "overlay/search",
        "overlay/server-settings",
        "changelog",
    ])


def sanitize_filename(path_or_query: str) -> str:
    """Convierte una ruta/URL en un nombre de archivo seguro para Windows."""
    cleaned = path_or_query.strip("/")
    if not cleaned:
        return "index.html"
    safe_name = re.sub(r'[\\/:*?"<>|&=]', "_", cleaned)
    safe_name = re.sub(r'_+', '_', safe_name)
    if not safe_name.endswith(".html"):
        safe_name += ".html"
    return safe_name


class VendettaPlaywrightScraper:
    def __init__(self, config: PlaywrightScraperConfig):
        self.config = config
        self.config.output_dir.mkdir(parents=True, exist_ok=True)
        self.visited_urls: Set[str] = set()
        self.saved_pages: List[Dict[str, str | int]] = []

    async def login(self, page: Page) -> bool:
        """Navega a la pantalla de login, llena el servidor, usuario y contraseña y entra."""
        login_url = f"{self.config.base_url}/login"
        logger.info(f"Navegando a la página de login: {login_url}")
        
        await page.goto(login_url, wait_until="domcontentloaded")

        # Seleccionar servidor (ej. s1)
        if await page.locator('select[name="uni"]').count() > 0:
            logger.info(f"Seleccionando servidor '{self.config.server}'...")
            await page.select_option('select[name="uni"]', self.config.server)

        # Llenar credenciales (en Vendetta el campo es name, no username)
        logger.info(f"Ingresando credenciales para '{self.config.username}'...")
        await page.fill('input[name="name"]', self.config.username)
        await page.fill('input[name="password"]', self.config.password)

        # Enviar formulario
        logger.info("Enviando formulario de acceso...")
        await page.click('#vendetta-enter-btn, button[type="submit"]')

        try:
            # Esperar a que la URL cambie y no sea /login
            await page.wait_for_url(lambda u: "/login" not in u, timeout=15000)
            await page.wait_for_load_state("domcontentloaded")
            logger.info(f"¡Inicio de sesión exitoso! Página actual: {page.url}")
            return True
        except PlaywrightTimeout:
            # Verificar si hay mensaje de error visible
            error_el = await page.query_selector('.alert-danger, .error, .invalid-feedback')
            err_msg = await error_el.inner_text() if error_el else "Tiempo de espera agotado"
            logger.error(f"Fallo al iniciar sesión: {err_msg.strip()}")
            return False

    async def save_current_page(self, page: Page, url: str) -> Optional[dict]:
        """Extrae el contenido renderizado de la página actual y lo guarda en disco."""
        try:
            parsed = urllib.parse.urlparse(url)
            path_query = parsed.path
            if parsed.query:
                path_query += f"_{parsed.query}"

            filename = sanitize_filename(path_query)
            target_file = self.config.output_dir / filename

            html_content = await page.content()
            title = await page.title()

            target_file.write_text(html_content, encoding="utf-8")
            file_size = target_file.stat().st_size

            logger.info(f"✓ Guardado: {filename} ({file_size // 1024} KB) - {title.strip()}")

            record = {
                "url": url,
                "file": filename,
                "title": title.strip(),
                "size_bytes": file_size,
            }
            self.saved_pages.append(record)
            return record
        except Exception as e:
            logger.error(f"Error al guardar página {url}: {e}")
            return None

    async def extract_page_links(self, page: Page) -> Set[str]:
        """Extrae todos los hipervínculos internos del juego."""
        links = await page.eval_on_selector_all("a[href]", "elements => elements.map(e => e.href)")
        internal_links = set()
        base_domain = urllib.parse.urlparse(self.config.base_url).netloc

        ignored_words = ["logout", "lang/", "javascript:", "#", "abandon", "destroy", "cancel"]

        for href in links:
            if not href or any(w in href for w in ignored_words):
                continue
            parsed = urllib.parse.urlparse(href)
            if parsed.netloc == base_domain:
                clean_url = urllib.parse.urlunparse((
                    parsed.scheme,
                    parsed.netloc,
                    parsed.path,
                    "",
                    parsed.query,
                    ""
                ))
                internal_links.add(clean_url)

        return internal_links

    async def run(self, explore_all_links: bool = True) -> None:
        """Inicia el navegador y procesa todas las subpáginas."""
        async with async_playwright() as p:
            browser: Browser = await p.chromium.launch(
                headless=self.config.headless,
                slow_mo=self.config.slow_mo_ms
            )
            context = await browser.new_context(
                viewport=self.config.viewport,
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
                )
            )
            page = await context.new_page()

            # 1. Login
            if not await self.login(page):
                logger.error("Cancelando operación por fallo de login.")
                await browser.close()
                return

            # 2. Cola de navegación inicial
            urls_to_visit: List[str] = [page.url]
            for cp in self.config.core_pages:
                full_u = urllib.parse.urljoin(self.config.base_url, cp)
                if full_u not in urls_to_visit:
                    urls_to_visit.append(full_u)

            # 3. Guardar la página actual (overview)
            await self.save_current_page(page, page.url)
            self.visited_urls.add(page.url)

            # Si se desea descubrir enlaces profundos de la navegación inicial
            if explore_all_links:
                initial_links = await self.extract_page_links(page)
                for lk in initial_links:
                    if lk not in urls_to_visit and lk not in self.visited_urls:
                        urls_to_visit.append(lk)

            logger.info(f"Total de páginas a procesar: {len(urls_to_visit)}")

            # 4. Navegar por cada subpágina
            for idx, target_url in enumerate(urls_to_visit, start=1):
                if target_url in self.visited_urls:
                    continue
                self.visited_urls.add(target_url)

                logger.info(f"[{idx}/{len(urls_to_visit)}] Navegando a: {target_url}")
                try:
                    response = await page.goto(target_url, wait_until="domcontentloaded", timeout=20000)
                    # Pequeña pausa para asegurar carga dinámica de componentes
                    await page.wait_for_timeout(400)

                    # Si fuimos devueltos al login, intentar re-autenticar o detener
                    if "/login" in page.url and "/login" not in target_url:
                        logger.warning(f"Redirección a login detectada al acceder a {target_url}. Reintentando login...")
                        if not await self.login(page):
                            break
                        continue

                    await self.save_current_page(page, target_url)

                    # Descubrir más enlaces si aún estamos explorando
                    if explore_all_links:
                        new_links = await self.extract_page_links(page)
                        for nlk in new_links:
                            if nlk not in urls_to_visit and nlk not in self.visited_urls:
                                urls_to_visit.append(nlk)

                except PlaywrightTimeout:
                    logger.warning(f"Timeout al cargar {target_url}. Intentando guardar estado actual...")
                    await self.save_current_page(page, target_url)
                except Exception as e:
                    logger.error(f"Error procesando {target_url}: {e}")

            # 5. Generar manifiesto JSON
            manifest_path = self.config.output_dir / "manifest.json"
            manifest_data = {
                "total_pages": len(self.saved_pages),
                "pages": self.saved_pages,
            }
            manifest_path.write_text(json.dumps(manifest_data, indent=2, ensure_ascii=False), encoding="utf-8")

            logger.info(f"✓ Finalizado. {len(self.saved_pages)} páginas guardadas en: {self.config.output_dir.resolve()}")
            await browser.close()


async def main():
    config = PlaywrightScraperConfig()
    scraper = VendettaPlaywrightScraper(config)
    await scraper.run(explore_all_links=True)


if __name__ == "__main__":
    asyncio.run(main())