"""
Script de scraping y respaldo de páginas HTML de Vendetta Legacy.
Utiliza requests y BeautifulSoup para una descarga rápida, limpia y estructurada.

Credenciales configuradas:
Usuario: Bomberox
Servidor: s1
"""

import os
import re
import time
import json
import logging
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Set, List, Dict, Optional
import requests
from bs4 import BeautifulSoup

# Configuración de logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S"
)
logger = logging.getLogger("VendettaScraper")


@dataclass
class ScraperConfig:
    base_url: str = "https://vendettalegacy.es"
    username: str = "Bomberox"
    password: str = "25Qkg4PrHZwVXt4"
    server: str = "s1"
    output_dir: Path = Path("vendetta_html_backup")
    request_delay: float = 0.5  # Pausa respetuosa entre peticiones
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
    # Lista inicial de rutas clave
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


@dataclass
class PageResult:
    url: str
    relative_path: str
    file_path: str
    status_code: int
    title: str
    size_bytes: int


class VendettaScraper:
    def __init__(self, config: ScraperConfig):
        self.config = config
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": self.config.user_agent,
            "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        })
        self.config.output_dir.mkdir(parents=True, exist_ok=True)
        self.visited_urls: Set[str] = set()
        self.results: List[PageResult] = []

    def sanitize_filename(self, path_or_query: str) -> str:
        """Convierte una ruta/URL en un nombre de archivo válido para Windows."""
        cleaned = path_or_query.strip("/")
        if not cleaned:
            return "index.html"
        # Reemplazar caracteres no permitidos en archivos Windows
        safe_name = re.sub(r'[\\/:*?"<>|&=]', "_", cleaned)
        safe_name = re.sub(r'_+', '_', safe_name)
        if not safe_name.endswith(".html"):
            safe_name += ".html"
        return safe_name

    def login(self) -> bool:
        """Obtiene token CSRF e inicia sesión con el servidor correcto."""
        login_url = f"{self.config.base_url}/login"
        logger.info(f"Obteniendo formulario de login: {login_url}")
        
        try:
            r = self.session.get(login_url, timeout=15)
            r.raise_for_status()
        except requests.RequestException as e:
            logger.error(f"Error al conectar con la página de inicio: {e}")
            return False

        soup = BeautifulSoup(r.text, "html.parser")
        token_input = soup.find("input", {"name": "_token"})
        if not token_input or not token_input.get("value"):
            logger.error("No se encontró el token CSRF en el formulario.")
            return False

        csrf_token = token_input["value"]
        logger.info(f"Token CSRF obtenido. Autenticando usuario '{self.config.username}' en servidor '{self.config.server}'...")

        payload = {
            "_token": csrf_token,
            "uni": self.config.server,
            "name": self.config.username,
            "password": self.config.password,
        }

        try:
            resp = self.session.post(login_url, data=payload, allow_redirects=True, timeout=15)
            resp.raise_for_status()
        except requests.RequestException as e:
            logger.error(f"Error durante el envío de credenciales: {e}")
            return False

        # Verificación de inicio de sesión exitoso
        if "/login" not in resp.url and (resp.status_code == 200 or "overview" in resp.url):
            logger.info(f"¡Inicio de sesión exitoso! Redirigido a: {resp.url}")
            return True

        # Inspeccionar posibles errores en el HTML
        soup_resp = BeautifulSoup(resp.text, "html.parser")
        errors = [
            e.get_text(strip=True)
            for e in soup_resp.find_all(class_=lambda c: c and any(k in c for k in ["error", "alert", "invalid"]))
        ]
        logger.error(f"Fallo en la autenticación. Errores detectados: {errors if errors else 'Credenciales o servidor rechazados'}")
        return False

    def discover_links(self, html_content: str) -> Set[str]:
        """Extrae enlaces internos válidos del HTML de una página."""
        discovered = set()
        soup = BeautifulSoup(html_content, "html.parser")
        
        # Ignorar rutas no deseadas (logout, cambios de idioma, llamadas de acción javascript)
        ignored_patterns = ["logout", "lang/", "javascript:", "#", "abandon", "destroy", "cancel"]

        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            if not href or any(ign in href for ign in ignored_patterns):
                continue

            full_url = urllib.parse.urljoin(self.config.base_url, href)
            parsed = urllib.parse.urlparse(full_url)

            # Mantenerse dentro del dominio
            if parsed.netloc == urllib.parse.urlparse(self.config.base_url).netloc:
                clean_url = urllib.parse.urlunparse((
                    parsed.scheme,
                    parsed.netloc,
                    parsed.path,
                    "",
                    parsed.query,
                    ""
                ))
                discovered.add(clean_url)

        return discovered

    def save_page(self, url: str) -> Optional[PageResult]:
        """Descarga una URL y guarda su contenido HTML en disco."""
        if url in self.visited_urls:
            return None
        self.visited_urls.add(url)

        parsed = urllib.parse.urlparse(url)
        path_query = parsed.path
        if parsed.query:
            path_query += f"_{parsed.query}"

        filename = self.sanitize_filename(path_query)
        target_file = self.config.output_dir / filename

        try:
            time.sleep(self.config.request_delay)
            resp = self.session.get(url, timeout=15)
            
            # Detectar si fuimos redirigidos a login
            if "/login" in resp.url and "/login" not in url:
                logger.warning(f"Sesión expirada o redirigida a login al acceder a: {url}")
                return None

            soup = BeautifulSoup(resp.text, "html.parser")
            title = soup.title.string.strip() if soup.title and soup.title.string else "Sin título"

            target_file.write_text(resp.text, encoding="utf-8")
            file_size = target_file.stat().st_size

            logger.info(f"✓ [{resp.status_code}] {filename} ({file_size // 1024} KB) - {title}")

            result = PageResult(
                url=url,
                relative_path=parsed.path + (f"?{parsed.query}" if parsed.query else ""),
                file_path=str(target_file),
                status_code=resp.status_code,
                title=title,
                size_bytes=file_size,
            )
            self.results.append(result)
            return result

        except Exception as e:
            logger.error(f"✗ Error al descargar {url}: {e}")
            return None

    def run(self, deep_crawl: bool = True) -> None:
        """Ejecuta el proceso completo de autenticación y respaldo."""
        if not self.login():
            logger.error("Cancelando proceso debido a fallo en login.")
            return

        urls_to_crawl: Set[str] = set()

        # Añadir páginas principales predefinidas
        for page in self.config.core_pages:
            full_url = urllib.parse.urljoin(self.config.base_url, page)
            urls_to_crawl.add(full_url)

        logger.info(f"Iniciando descarga de {len(urls_to_crawl)} páginas principales...")

        # Primera pasada: descargar páginas núcleo y recopilar subenlaces
        subpages_queue = list(urls_to_crawl)
        for url in subpages_queue:
            result = self.save_page(url)
            if deep_crawl and result and result.status_code == 200:
                # Leer el archivo guardado para extraer subpáginas profundas
                html_text = Path(result.file_path).read_text(encoding="utf-8")
                new_links = self.discover_links(html_text)
                for link in new_links:
                    if link not in self.visited_urls and link not in urls_to_crawl:
                        urls_to_crawl.add(link)
                        subpages_queue.append(link)

        # Guardar manifiesto JSON con el resumen de todas las páginas
        manifest_path = self.config.output_dir / "manifest.json"
        manifest_data = {
            "total_pages": len(self.results),
            "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "pages": [
                {
                    "url": r.url,
                    "file": os.path.basename(r.file_path),
                    "title": r.title,
                    "size_bytes": r.size_bytes,
                    "status_code": r.status_code
                }
                for r in self.results
            ]
        }
        manifest_path.write_text(json.dumps(manifest_data, indent=2, ensure_ascii=False), encoding="utf-8")
        
        logger.info(f"Proceso finalizado con éxito. Total páginas guardadas: {len(self.results)}")
        logger.info(f"Directorio de guardado: {self.config.output_dir.resolve()}")
        logger.info(f"Manifiesto generado en: {manifest_path.resolve()}")


if __name__ == "__main__":
    config = ScraperConfig()
    scraper = VendettaScraper(config)
    scraper.run(deep_crawl=True)