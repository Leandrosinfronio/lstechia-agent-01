"""
LS.IA Agent 01 - Nucleo do Agente LinkedIn
Versao: 2.0
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import subprocess
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

import httpx
from dotenv import load_dotenv
from playwright.async_api import (
    BrowserContext,
    Page,
    Playwright,
    TimeoutError as PWTimeout,
    async_playwright,
)


BASE_DIR = Path(__file__).parent
load_dotenv(BASE_DIR / ".env")

PERFIL_PATH = BASE_DIR / "perfil.json"
VAGAS_APLICADAS_PATH = BASE_DIR / "vagas_aplicadas.json"
LOG_FILE = BASE_DIR / "agent.log"
LOG_MAX_BYTES = int(os.getenv("LOG_MAX_BYTES", "1000000"))
LOG_KEEP_LINES = int(os.getenv("LOG_KEEP_LINES", "800"))

CHROME_USER_DATA_DIR = os.getenv("CHROME_USER_DATA_DIR", str(BASE_DIR / "chrome_agent_profile_real"))
CHROME_PROFILE_NAME = os.getenv("CHROME_PROFILE_NAME", "Default")

LINKEDIN_PERFIL_URL = os.getenv("LINKEDIN_PERFIL_URL", "https://www.linkedin.com/in/")
LINKEDIN_JOBS_URL = "https://www.linkedin.com/jobs/"

CDP_HOST = "127.0.0.1"
CDP_PORT = int(os.getenv("CDP_PORT", "9222"))
CDP_URL = f"http://{CDP_HOST}:{CDP_PORT}"

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434/api/generate")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3.2:3b")
USAR_OLLAMA = os.getenv("USAR_OLLAMA", "false").lower() in ("1", "true", "sim", "yes")
OLLAMA_TIMEOUT_S = float(os.getenv("OLLAMA_TIMEOUT_S", "5"))

GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.1-8b-instant")
USAR_GROQ = os.getenv("USAR_GROQ", "false").lower() in ("1", "true", "sim", "yes")
GROQ_TIMEOUT_S = float(os.getenv("GROQ_TIMEOUT_S", "8"))
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"

PESQUISAS_PADRAO = [
    "analista de TI",
    "analista de sistemas",
    "analista de infraestrutura",
    "analista de infraestrutura TI",
    "analista de suporte TI",
    "analista cloud",
    "analista devops",
    "analista de IA",
    "analista de inteligencia artificial",
    "analista de automacao",
    "analista de seguranca da informacao",
    "analista cyber security",
    "desenvolvedor python",
    "desenvolvedor de sistemas",
    "infraestrutura de TI",
    "cloud computing",
]

AUTO_ENVIAR_CANDIDATURA = True
MODO_ASSISTIDO = True  # destaque visual + traz Chrome pra frente
DELAY_VISUAL_ASSISTIDO_S = float(os.getenv("DELAY_VISUAL_ASSISTIDO_S", "0.45"))
CURSOR_VISUAL_ID = "lsia-agent-cursor"
CURSOR_PULSE_ID = "lsia-agent-cursor-pulse"
MAX_VAGAS_POR_CICLO = 25
MAX_ETAPAS_CANDIDATURA = 10
ESPERA_ENTRE_CICLOS_S = (20, 35)
ESPERA_ENTRE_VAGAS_S = (1.5, 3.5)
ESPERA_ENTRE_BUSCAS_S = (3, 6)

TIPOS_DE_VAGA = {
    "ia": [
        "ia", "ai", "artificial intelligence", "inteligencia artificial",
        "machine learning", "ml", "llm", "genai", "dados", "data",
        "python", "automacao", "automation",
    ],
    "infraestrutura": [
        "infraestrutura", "infrastructure", "cloud", "aws", "azure",
        "devops", "linux", "windows server", "redes", "network",
        "servidor", "server", "seguranca", "security", "cyber",
        "suporte", "sre", "sysadmin",
    ],
}

TECNOLOGIAS_ESPECIFICAS = [
    "sap", "s/4hana", "s4hana", "ecc", "sap sd", "sap mm", "wis web",
    "abap", "salesforce", "oracle ebs", "totvs", "protheus",
]


def _agora() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _log_arquivo(msg: str) -> None:
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"[{_agora()}] {msg}\n")
        if LOG_FILE.stat().st_size > LOG_MAX_BYTES:
            linhas = LOG_FILE.read_text(encoding="utf-8", errors="ignore").splitlines()
            ultimas = linhas[-LOG_KEEP_LINES:]
            LOG_FILE.write_text("\n".join(ultimas) + "\n", encoding="utf-8")
    except Exception:
        pass


@dataclass
class EstadoAgente:
    rodando: bool = False
    parando: bool = False
    candidaturas_enviadas: int = 0
    vagas_analisadas: int = 0
    erro_atual: Optional[str] = None
    iniciado_em: Optional[str] = None
    ultima_atividade: Optional[str] = None
    pesquisa_atual: Optional[str] = None
    vagas_aplicadas: set = field(default_factory=set)
    vagas_tentadas: set = field(default_factory=set)

    def snapshot(self) -> dict:
        return {
            "rodando": self.rodando,
            "parando": self.parando,
            "candidaturas_enviadas": self.candidaturas_enviadas,
            "vagas_analisadas": self.vagas_analisadas,
            "erro_atual": self.erro_atual,
            "iniciado_em": self.iniciado_em,
            "ultima_atividade": self.ultima_atividade,
            "pesquisa_atual": self.pesquisa_atual,
            "total_aplicadas_persistente": len(self.vagas_aplicadas),
        }


class AgenteLinkedIn:
    """Agente unico e isolado. Pode coexistir com agent-02, agent-03 etc."""

    def __init__(self, log_callback: Callable[[str], None] = print, nome: str = "agent-01"):
        self.log_callback = log_callback
        self.nome = nome
        self.estado = EstadoAgente()
        self.stop_event = asyncio.Event()
        self.perfil: dict = {}
        self._playwright: Optional[Playwright] = None
        self._context: Optional[BrowserContext] = None
        self._page: Optional[Page] = None
        self._carregar_vagas_aplicadas()

    def log(self, msg: str) -> None:
        carimbo = f"[{_agora()}][{self.nome}] {msg}"
        self.estado.ultima_atividade = msg
        self.log_callback(carimbo)
        _log_arquivo(carimbo)

    def _carregar_vagas_aplicadas(self) -> None:
        if VAGAS_APLICADAS_PATH.exists():
            try:
                dados = json.loads(VAGAS_APLICADAS_PATH.read_text(encoding="utf-8"))
                if isinstance(dados, list):
                    self.estado.vagas_aplicadas = set(dados)
            except Exception:
                self.estado.vagas_aplicadas = set()

    def _salvar_vagas_aplicadas(self) -> None:
        try:
            VAGAS_APLICADAS_PATH.write_text(
                json.dumps(sorted(self.estado.vagas_aplicadas), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            self.log(f"AVISO: falha ao persistir vagas: {e}")

    def _carregar_perfil(self) -> None:
        if not PERFIL_PATH.exists():
            raise FileNotFoundError(f"Perfil nao encontrado em {PERFIL_PATH}")
        self.perfil = json.loads(PERFIL_PATH.read_text(encoding="utf-8"))

    async def _cdp_disponivel(self, timeout_s: float = 5.0) -> bool:
        """Testa o CDP com retry interno e timeout generoso."""
        for tentativa in range(3):
            try:
                async with httpx.AsyncClient(timeout=timeout_s) as client:
                    r = await client.get(f"{CDP_URL}/json/version")
                    if r.status_code == 200:
                        return True
            except Exception:
                pass
            await asyncio.sleep(1)
        return False

    async def _esperar_cdp(self, timeout_s: int = 30) -> bool:
        inicio = time.time()
        while time.time() - inicio < timeout_s:
            if self.stop_event.is_set():
                return False
            if await self._cdp_disponivel(timeout_s=2.0):
                self.log(f"CDP disponivel em {CDP_URL}")
                return True
            await asyncio.sleep(1)
        return False

    def _achar_chrome_exe(self) -> Optional[str]:
        candidatos = [
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
        ]
        for caminho in candidatos:
            if caminho and Path(caminho).exists():
                return caminho
        return None

    def _matar_chrome(self) -> None:
        """Mata todos os processos chrome.exe e espera realmente cairem."""
        try:
            subprocess.run(
                ["taskkill", "/F", "/IM", "chrome.exe", "/T"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        except Exception as e:
            self.log(f"AVISO: falha ao encerrar Chrome: {e}")

    def _limpar_singleton_locks(self) -> None:
        """Apaga SingletonLock/Cookie/Socket do perfil para evitar reuso silencioso."""
        for nome in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
            alvo = Path(CHROME_USER_DATA_DIR) / nome
            try:
                if alvo.exists():
                    alvo.unlink()
                    self.log(f"Lock removido: {nome}")
            except Exception as e:
                self.log(f"AVISO: nao consegui remover {nome}: {e}")

    def _abrir_chrome_com_debug(self) -> None:
        chrome_exe = self._achar_chrome_exe()
        if not chrome_exe:
            raise RuntimeError("Google Chrome nao encontrado no sistema.")
        self.log(f"chrome.exe encontrado: {chrome_exe}")
        args = [
            chrome_exe,
            f"--remote-debugging-port={CDP_PORT}",
            "--remote-allow-origins=*",
            f"--user-data-dir={CHROME_USER_DATA_DIR}",
            f"--profile-directory={CHROME_PROFILE_NAME}",
            LINKEDIN_PERFIL_URL,
        ]
        creationflags = 0x00000008 | 0x00000200
        subprocess.Popen(args, creationflags=creationflags, close_fds=True)
        self.log("subprocess.Popen disparado para o Chrome em modo debug.")

    async def _garantir_chrome_em_modo_debug(self) -> None:
        """
        Politica nova (a partir de v2.1):
        NUNCA mata ou reinicia o Chrome do usuario. Se o CDP nao responder,
        instrui o usuario a rodar o launcher .bat antes. Isso evita perder a
        sessao do LinkedIn por race conditions de profile lock no Windows.
        """
        self.log("Testando CDP em 127.0.0.1:9222 (com retry)...")
        if await self._cdp_disponivel(timeout_s=5.0):
            self.log("CDP RESPONDEU. Reaproveitando o Chrome real.")
            return

        self.log("CDP nao respondeu na primeira rodada. Tentando mais 20s...")
        if await self._esperar_cdp(timeout_s=20):
            return

        raise RuntimeError(
            "ECONNREFUSED 9222: o Chrome em modo debug nao esta rodando. "
            "Abra outro PowerShell, va para a pasta do projeto e rode "
            ".\\abrir_chrome_debug.bat e confira que o teste no final "
            "imprime 'OK: Chrome em modo debug RESPONDENDO em 127.0.0.1:9222'. "
            "Depois clique LIGAR de novo."
        )
    async def _conectar_playwright(self) -> None:
        self._playwright = await async_playwright().start()
        browser = await self._playwright.chromium.connect_over_cdp(CDP_URL)
        if not browser.contexts:
            raise RuntimeError("CDP conectado mas nenhum contexto Chrome disponivel.")
        contextos_validos = [c for c in browser.contexts if c.pages]
        self._context = contextos_validos[0] if contextos_validos else browser.contexts[0]
        for p in self._context.pages:
            try:
                if "linkedin.com" in (p.url or ""):
                    self._page = p
                    break
            except Exception:
                continue
        if self._page is None:
            self._page = await self._context.new_page()
        # Modo assistido: traz a janela do Chrome pra frente
        try:
            await self._page.bring_to_front()
        except Exception:
            pass
        self.log("Playwright conectado ao Chrome real via CDP.")

    async def _desconectar_playwright(self) -> None:
        try:
            if self._playwright:
                await self._playwright.stop()
        except Exception as e:
            self.log(f"AVISO: erro ao finalizar Playwright: {e}")
        finally:
            self._playwright = None
            self._context = None
            self._page = None

    async def _sleep_jitter(self, intervalo) -> None:
        baixo, alto = intervalo
        total = random.uniform(baixo, alto)
        passos = max(1, int(total / 0.25))
        for _ in range(passos):
            if self.stop_event.is_set():
                return
            await asyncio.sleep(0.25)

    async def _fechar_popups(self) -> int:
        if not self._page:
            return 0
        fechados = 0
        seletores = [
            "button[aria-label='Fechar']",
            "button[aria-label='Dismiss']",
            "button[aria-label='Close']",
            "button[aria-label='Descartar']",
            "button:has-text('Agora nao')",
            "button:has-text('Not now')",
            "button:has-text('Aceitar')",
            "button:has-text('Got it')",
        ]
        for sel in seletores:
            try:
                loc = self._page.locator(sel)
                count = await loc.count()
                for i in range(min(count, 3)):
                    try:
                        await loc.nth(i).click(timeout=2000)
                        fechados += 1
                        await asyncio.sleep(0.5)
                    except Exception:
                        pass
            except Exception:
                pass
        return fechados

    async def _esta_logado(self) -> bool:
        if not self._page or not self._context:
            return False
        try:
            url = (self._page.url or "").lower()
            if any(t in url for t in ["authwall", "checkpoint", "/login", "/uas/login"]):
                return False
            cookies = await self._context.cookies("https://www.linkedin.com")
            tem_li_at = any(c.get("name") == "li_at" and c.get("value") for c in cookies)
            if tem_li_at:
                return True
            sel = "header.global-nav, nav.global-nav"
            return await self._page.locator(sel).count() > 0
        except Exception:
            return False

    async def _ir_para(self, url: str) -> None:
        if not self._page:
            return
        try:
            await self._trazer_chrome_para_frente()
            await self._page.goto(url, wait_until="domcontentloaded", timeout=45000)
            await self._pausa_visual()
        except PWTimeout:
            self.log(f"AVISO: timeout ao abrir {url}")
        except Exception as e:
            self.log(f"AVISO: erro ao abrir {url}: {e}")

    async def _trazer_chrome_para_frente(self) -> None:
        """Modo assistido: tenta ativar a aba/janela controlada pelo CDP."""
        if not MODO_ASSISTIDO or not self._page:
            return
        try:
            await self._page.bring_to_front()
            session = await self._context.new_cdp_session(self._page) if self._context else None
            if session:
                await session.send("Page.bringToFront")
        except Exception:
            pass
        self._trazer_janela_windows_para_frente()
        await self._instalar_cursor_visual()

    def _trazer_janela_windows_para_frente(self) -> None:
        if os.name != "nt":
            return
        try:
            import ctypes

            user32 = ctypes.windll.user32
            handles = []

            def enum_handler(hwnd, _lparam):
                if not user32.IsWindowVisible(hwnd):
                    return True
                tamanho = user32.GetWindowTextLengthW(hwnd)
                if tamanho <= 0:
                    return True
                buffer = ctypes.create_unicode_buffer(tamanho + 1)
                user32.GetWindowTextW(hwnd, buffer, tamanho + 1)
                titulo = buffer.value.lower()
                if "linkedin" in titulo and "chrome" in titulo:
                    handles.insert(0, hwnd)
                elif "chrome" in titulo:
                    handles.append(hwnd)
                return True

            callback = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)(enum_handler)
            user32.EnumWindows(callback, 0)
            if handles:
                hwnd = handles[0]
                user32.ShowWindow(hwnd, 9)
                user32.SetForegroundWindow(hwnd)
        except Exception:
            pass

    async def _pausa_visual(self, segundos: float = DELAY_VISUAL_ASSISTIDO_S) -> None:
        if MODO_ASSISTIDO:
            await asyncio.sleep(segundos)

    async def _destacar(self, locator) -> None:
        """Modo assistido: pinta um contorno laranja no elemento antes da acao."""
        if not MODO_ASSISTIDO:
            return
        try:
            await self._trazer_chrome_para_frente()
            await locator.scroll_into_view_if_needed(timeout=2000)
            await self._mover_cursor_visual_para(locator)
            await locator.evaluate(
                "(el) => { el.style.outline = '3px solid #f59e0b'; "
                "el.style.boxShadow = '0 0 20px #f59e0b'; "
                "el.style.transition = 'outline 120ms ease, box-shadow 120ms ease'; }"
            )
            await self._pausa_visual()
            await locator.evaluate(
                "(el) => { el.style.outline = ''; el.style.boxShadow = ''; }"
            )
        except Exception:
            pass

    async def _instalar_cursor_visual(self) -> None:
        if not MODO_ASSISTIDO or not self._page:
            return
        try:
            await self._page.evaluate(
                f"""
                () => {{
                    if (document.getElementById('{CURSOR_VISUAL_ID}')) return;
                    const style = document.createElement('style');
                    style.id = '{CURSOR_VISUAL_ID}-style';
                    style.textContent = `
                        #{CURSOR_VISUAL_ID} {{
                            position: fixed;
                            left: 24px;
                            top: 24px;
                            width: 34px;
                            height: 34px;
                            border-radius: 999px;
                            display: grid;
                            place-items: center;
                            background: #38bdf8;
                            color: #020617;
                            border: 2px solid #ffffff;
                            box-shadow: 0 0 0 3px rgba(56,189,248,.28), 0 10px 28px rgba(0,0,0,.35);
                            font: 900 13px/1 "Segoe UI", Arial, sans-serif;
                            z-index: 2147483647;
                            pointer-events: none;
                            transform: translate(-50%, -50%);
                            transition: left 420ms ease, top 420ms ease, transform 120ms ease;
                        }}
                        #{CURSOR_PULSE_ID} {{
                            position: fixed;
                            left: 24px;
                            top: 24px;
                            width: 10px;
                            height: 10px;
                            border-radius: 999px;
                            border: 3px solid #f59e0b;
                            opacity: 0;
                            z-index: 2147483646;
                            pointer-events: none;
                            transform: translate(-50%, -50%) scale(1);
                        }}
                        #{CURSOR_PULSE_ID}.on {{
                            animation: lsiaAgentPulse 520ms ease-out;
                        }}
                        @keyframes lsiaAgentPulse {{
                            0% {{ opacity: .95; transform: translate(-50%, -50%) scale(1); }}
                            100% {{ opacity: 0; transform: translate(-50%, -50%) scale(5.2); }}
                        }}
                    `;
                    document.documentElement.appendChild(style);
                    const cursor = document.createElement('div');
                    cursor.id = '{CURSOR_VISUAL_ID}';
                    cursor.textContent = 'LS';
                    const pulse = document.createElement('div');
                    pulse.id = '{CURSOR_PULSE_ID}';
                    document.documentElement.appendChild(pulse);
                    document.documentElement.appendChild(cursor);
                }}
                """
            )
        except Exception:
            pass

    async def _mover_cursor_visual_para(self, locator) -> None:
        if not MODO_ASSISTIDO or not self._page:
            return
        try:
            box = await locator.bounding_box(timeout=2000)
            if not box:
                return
            x = box["x"] + box["width"] / 2
            y = box["y"] + box["height"] / 2
            await self._page.mouse.move(x, y, steps=18)
            await self._page.evaluate(
                f"""
                ([x, y]) => {{
                    const cursor = document.getElementById('{CURSOR_VISUAL_ID}');
                    const pulse = document.getElementById('{CURSOR_PULSE_ID}');
                    if (!cursor || !pulse) return;
                    cursor.style.left = `${{x}}px`;
                    cursor.style.top = `${{y}}px`;
                    pulse.style.left = `${{x}}px`;
                    pulse.style.top = `${{y}}px`;
                    cursor.style.transform = 'translate(-50%, -50%) scale(1.14)';
                    window.setTimeout(() => {{
                        cursor.style.transform = 'translate(-50%, -50%) scale(1)';
                    }}, 140);
                    pulse.classList.remove('on');
                    void pulse.offsetWidth;
                    pulse.classList.add('on');
                }}
                """,
                [x, y],
            )
            await self._pausa_visual(0.45)
        except Exception:
            pass

    async def _click_assistido(self, locator, descricao: str, timeout: int = 8000) -> bool:
        """Destaca, espera e clica apenas se o elemento estiver realmente visivel."""
        try:
            await self._trazer_chrome_para_frente()
            await locator.wait_for(state="visible", timeout=timeout)
            await self._destacar(locator)
            self.log(f"Acao assistida: clicando em {descricao}.")
            try:
                await locator.click(timeout=timeout)
            except Exception:
                await locator.evaluate("(el) => el.click()")
            await self._pausa_visual(0.8)
            return True
        except Exception as e:
            self.log(f"AVISO: nao consegui clicar em {descricao}: {e}")
            return False

    async def _primeiro_visivel(self, locator, limite: int = 12):
        try:
            count = min(await locator.count(), limite)
            for i in range(count):
                item = locator.nth(i)
                try:
                    if await item.is_visible(timeout=800):
                        return item
                except Exception:
                    continue
        except Exception:
            return None
        return None

    @staticmethod
    def _normalizar_texto(texto: str) -> str:
        texto = unicodedata.normalize("NFKD", texto or "")
        texto = "".join(c for c in texto if not unicodedata.combining(c))
        return texto.lower().strip()

    async def executar(self) -> None:
        self.estado.rodando = True
        self.estado.parando = False
        self.estado.iniciado_em = _agora()
        self.estado.erro_atual = None
        self.stop_event.clear()
        self.log(f"Inicializando {self.nome}...")
        try:
            self._carregar_perfil()
            await self._garantir_chrome_em_modo_debug()
            await self._conectar_playwright()
            await self._ir_para(LINKEDIN_PERFIL_URL)
            await self._sleep_jitter((3, 6))
            await self._fechar_popups()
            if not await self._esta_logado():
                self.estado.erro_atual = "LinkedIn deslogado nessa janela do Chrome."
                self.log("ERRO: LinkedIn nao esta logado. Faca login manualmente e clique LIGAR de novo.")
                return
            self.log(f"Login confirmado para {self.perfil.get('nome', 'usuario')}.")
            await self._loop_de_candidaturas()
        except asyncio.CancelledError:
            self.log("Execucao cancelada.")
            raise
        except Exception as e:
            self.estado.erro_atual = str(e)
            self.log(f"ERRO fatal: {e}")
        finally:
            await self._fechar_modal()
            await self._desconectar_playwright()
            self.estado.rodando = False
            self.estado.parando = False
            self.log("Agente parado.")

    async def _loop_de_candidaturas(self) -> None:
        while not self.stop_event.is_set():
            for termo in PESQUISAS_PADRAO:
                if self.stop_event.is_set():
                    return
                self.estado.pesquisa_atual = termo
                self.log(f"Termo: '{termo}'")
                try:
                    await self._buscar_e_processar(termo)
                except Exception as e:
                    self.log(f"AVISO: falha no termo '{termo}': {e}")
                    if not await self._esta_logado():
                        self.log("Sessao LinkedIn caiu. Encerrando ciclo.")
                        self.estado.erro_atual = "Sessao LinkedIn caiu."
                        return
                await self._sleep_jitter(ESPERA_ENTRE_BUSCAS_S)
            self.log("Ciclo completo. Aguardando antes do proximo...")
            await self._sleep_jitter(ESPERA_ENTRE_CICLOS_S)

    async def _buscar_e_processar(self, termo: str) -> None:
        await self._ir_para(self._url_busca(termo))
        await self._sleep_jitter((4, 8))
        await self._fechar_popups()
        if not await self._esta_logado():
            raise RuntimeError("Nao esta logado.")
        try:
            await self._page.mouse.wheel(0, 2500)
        except Exception:
            pass
        await asyncio.sleep(2)
        seletores_card = [
            "a.job-card-container__link",
            "a.job-card-list__title",
            "div.job-card-container a",
        ]
        vagas = []
        for sel in seletores_card:
            try:
                vagas = await self._page.locator(sel).all()
                if vagas:
                    break
            except Exception:
                continue
        if not vagas:
            self.log("Nenhum card carregou para esse termo.")
            return
        self.log(f"{len(vagas)} cards listados. Processando ate {MAX_VAGAS_POR_CICLO}.")
        for vaga in vagas[:MAX_VAGAS_POR_CICLO]:
            if self.stop_event.is_set():
                return
            try:
                await self._fechar_modal()
                link = await vaga.get_attribute("href")
                if not link:
                    continue
                chave = link.split("?")[0]
                if chave in self.estado.vagas_aplicadas or chave in self.estado.vagas_tentadas:
                    continue
                if not await self._click_assistido(vaga, "card da vaga", timeout=8000):
                    continue
                await self._sleep_jitter((1, 2))
                await self._fechar_popups()
                self.estado.vagas_analisadas += 1
                texto = await self._page.locator("body").inner_text()
                if not self._tem_candidatura_simplificada(texto):
                    continue
                tipo_vaga = self._tipo_de_vaga(texto)
                if not await self._ia_diz_compativel(texto, tipo_vaga):
                    continue
                self.log(f"Vaga compativel ({tipo_vaga}). Iniciando candidatura...")
                enviou = await self._tentar_candidatar(texto, tipo_vaga)
                self.estado.vagas_tentadas.add(chave)
                if enviou:
                    self.estado.candidaturas_enviadas += 1
                    self.estado.vagas_aplicadas.add(chave)
                    self._salvar_vagas_aplicadas()
                    self.log(f"Candidatura ENVIADA. Total nesta sessao: {self.estado.candidaturas_enviadas}")
                else:
                    self.log(f"Vaga ignorada nesta sessao: {chave}")
                    await self._fechar_modal()
                await self._sleep_jitter(ESPERA_ENTRE_VAGAS_S)
            except Exception as e:
                self.log(f"AVISO: erro ao processar card: {e}")
                await self._fechar_modal()

    def _url_busca(self, termo: str) -> str:
        from urllib.parse import quote_plus
        return (
            f"{LINKEDIN_JOBS_URL}search/"
            f"?keywords={quote_plus(termo)}"
            "&location=Brasil"
            "&f_AL=true"
            "&f_WT=2"
            "&sortBy=DD"
        )

    @staticmethod
    def _tem_candidatura_simplificada(texto: str) -> bool:
        t = texto.lower()
        return "candidatura simplificada" in t or "easy apply" in t

    def _tipo_de_vaga(self, texto_vaga: str) -> str:
        t = texto_vaga.lower()
        pontos = {
            tipo: sum(1 for termo in termos if termo in t)
            for tipo, termos in TIPOS_DE_VAGA.items()
        }
        if pontos["ia"] > pontos["infraestrutura"]:
            return "ia"
        if pontos["infraestrutura"] > 0:
            return "infraestrutura"
        return "geral"

    def _prompt_ia(self, texto_vaga: str) -> str:
        return (
            "Voce e um filtro de match de curriculo. Responda APENAS no formato:\n"
            "DECISAO: COMPATIVEL\nMOTIVO: <frase curta>\n"
            "ou\n"
            "DECISAO: NAO_COMPATIVEL\nMOTIVO: <frase curta>\n\n"
            f"PERFIL:\n{json.dumps(self.perfil, ensure_ascii=False)}\n\n"
            f"VAGA:\n{texto_vaga[:4500]}\n"
        )

    @staticmethod
    def _avaliar_resposta_ia(resposta: str) -> bool:
        decisao = resposta.upper()
        if "NAO_COMPATIVEL" in decisao or "NAO COMPATIVEL" in decisao:
            return False
        return "COMPATIVEL" in decisao

    async def _ia_groq(self, texto_vaga: str) -> bool:
        try:
            async with httpx.AsyncClient(timeout=GROQ_TIMEOUT_S) as client:
                r = await client.post(
                    GROQ_URL,
                    headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
                    json={
                        "model": GROQ_MODEL,
                        "messages": [{"role": "user", "content": self._prompt_ia(texto_vaga)}],
                        "max_tokens": 100,
                        "temperature": 0,
                    },
                )
                resposta = r.json()["choices"][0]["message"]["content"]
                self.log(f"Groq ({GROQ_MODEL}): {resposta[:80]}")
                return self._avaliar_resposta_ia(resposta)
        except Exception as e:
            self.log(f"AVISO: Groq indisponivel ({e}). Aprovado pela heuristica.")
            return True

    async def _ia_diz_compativel(self, texto_vaga: str, tipo_vaga: str) -> bool:
        if not self._fallback_heuristico(texto_vaga, tipo_vaga):
            return False

        if USAR_GROQ and GROQ_API_KEY:
            return await self._ia_groq(texto_vaga)

        if not USAR_OLLAMA:
            return True

        try:
            async with httpx.AsyncClient(timeout=OLLAMA_TIMEOUT_S) as client:
                r = await client.post(
                    OLLAMA_URL,
                    json={"model": OLLAMA_MODEL, "prompt": self._prompt_ia(texto_vaga), "stream": False},
                )
                resposta = r.json().get("response", "")
        except Exception as e:
            self.log(f"AVISO: Ollama indisponivel ({e}). Aprovado pela heuristica rapida.")
            return True
        return self._avaliar_resposta_ia(resposta)

    def _fallback_heuristico(self, texto_vaga: str, tipo_vaga: str = "geral") -> bool:
        t = texto_vaga.lower()
        palavras = TIPOS_DE_VAGA["ia"] + TIPOS_DE_VAGA["infraestrutura"]
        minimo = 1 if tipo_vaga in ("ia", "infraestrutura") else 2
        return sum(1 for p in palavras if p in t) >= minimo

    async def _tentar_candidatar(self, texto_vaga: str = "", tipo_vaga: str = "geral") -> bool:
        try:
            botao = self._page.locator(
                ".jobs-search__job-details--container button.jobs-apply-button, "
                ".jobs-details button.jobs-apply-button, "
                ".jobs-apply-button--top-card button.jobs-apply-button, "
                "button.jobs-apply-button"
            )
            botao_visivel = await self._primeiro_visivel(botao)
            if botao_visivel is None:
                return False
            aria = ((await botao_visivel.get_attribute("aria-label")) or "").lower()
            texto_botao = ((await botao_visivel.inner_text()) or "").lower()
            if "filtro" in aria or "filter" in aria or "candidatura simplificada" not in (aria + " " + texto_botao) and "easy apply" not in (aria + " " + texto_botao):
                self.log("AVISO: botao de candidatura simplificada nao apareceu no painel da vaga.")
                return False
            if not await self._click_assistido(botao_visivel, "botao Candidatura simplificada", timeout=8000):
                return False
            await self._sleep_jitter((0.7, 1.4))
            erros_consecutivos = 0
            _etapa_hash_anterior = ""
            _etapas_travadas = 0
            _revisar_consecutivo = 0
            for _etapa in range(1, MAX_ETAPAS_CANDIDATURA + 1):
                if self.stop_event.is_set():
                    return False
                await self._preencher_campos_modal()
                await self._selecionar_curriculo(texto_vaga, tipo_vaga)
                await asyncio.sleep(0.35)
                enviar_visivel = await self._botao_modal_por_texto(
                    ["Enviar candidatura", "Submit application", "Enviar"]
                )
                if enviar_visivel is not None:
                    if not AUTO_ENVIAR_CANDIDATURA:
                        await self._fechar_modal()
                        return False
                    if not await self._click_assistido(enviar_visivel, "botao Enviar candidatura", timeout=8000):
                        return False
                    await self._sleep_jitter((1, 2))
                    await self._fechar_modal()
                    return True
                revisar_visivel = await self._botao_modal_por_texto(["Revisar", "Review"])
                if revisar_visivel is not None:
                    _revisar_consecutivo += 1
                    if _revisar_consecutivo >= 5:
                        self.log("AVISO: loop em Revisar (5x). Descartando vaga.")
                        await self._fechar_modal()
                        return False
                    # Tenta preencher campos pendentes antes de clicar Revisar
                    await self._preencher_campos_modal()
                    if not await self._click_assistido(revisar_visivel, "botao Revisar", timeout=8000):
                        return False
                    await self._sleep_jitter((0.7, 1.4))
                    continue
                _revisar_consecutivo = 0
                proximo_btn = await self._botao_modal_por_texto(
                    ["Avançar", "Avancar", "Próximo", "Proximo", "Next", "Continuar", "Continue"]
                )
                if proximo_btn is None:
                    proximo_loc = self._page.locator(
                        "button:has-text('Avançar'), button:has-text('Próximo'), "
                        "button:has-text('Next'), button[aria-label*='Next']"
                    )
                    proximo_vis = await self._primeiro_visivel(proximo_loc)
                    if proximo_vis is not None:
                        await self._rolar_modal_ate_botao(proximo_vis)
                        proximo_btn = proximo_vis
                if proximo_btn is not None:
                    # Captura hash da etapa atual antes de avançar
                    try:
                        _hash_antes = (await self._modal_locator().inner_text())[:200]
                    except Exception:
                        _hash_antes = ""
                    if not await self._click_assistido(proximo_btn, "botao Proximo", timeout=8000):
                        return False
                    await self._sleep_jitter((0.7, 1.4))
                    # Detecta etapa travada (conteúdo idêntico ao anterior)
                    try:
                        _hash_depois = (await self._modal_locator().inner_text())[:200]
                    except Exception:
                        _hash_depois = ""
                    if _hash_antes and _hash_depois and _hash_antes == _hash_depois:
                        _etapas_travadas += 1
                        self.log(f"AVISO: etapa nao avancou ({_etapas_travadas}/2). Tentando preencher novamente...")
                        if _etapas_travadas >= 2:
                            self.log("AVISO: etapa travada 2x. Descartando vaga.")
                            await self._fechar_modal()
                            return False
                        await self._preencher_campos_modal()
                        await self._selecionar_curriculo(texto_vaga, tipo_vaga)
                        continue
                    else:
                        _etapas_travadas = 0
                        _etapa_hash_anterior = _hash_depois
                    if await self._tem_erros_validacao():
                        erros_consecutivos += 1
                        self.log(f"AVISO: campo obrigatorio vazio (tentativa {erros_consecutivos}/3). Tentando preencher...")
                        if erros_consecutivos >= 3:
                            self.log("AVISO: 3 erros consecutivos. Descartando vaga.")
                            await self._fechar_modal()
                            return False
                        await asyncio.sleep(0.5)
                        await self._preencher_campos_modal()
                        await self._selecionar_curriculo(texto_vaga, tipo_vaga)
                    else:
                        erros_consecutivos = 0
                    continue
                await self._fechar_modal()
                return False
            await self._fechar_modal()
            return False
        except Exception as e:
            self.log(f"AVISO: erro durante candidatura: {e}")
            await self._fechar_modal()
            return False

    async def _rolar_modal_para_baixo(self) -> None:
        if not self._page:
            return
        try:
            await self._page.evaluate(
                """
                () => {
                    const modal = document.querySelector('.jobs-easy-apply-modal, [role="dialog"], .artdeco-modal');
                    const body = modal?.querySelector('.jobs-easy-apply-modal__content, .artdeco-modal__content');
                    if (body) body.scrollTop = body.scrollHeight;
                    else modal?.scrollTo?.(0, modal.scrollHeight);
                }
                """
            )
            await self._pausa_visual(0.2)
        except Exception:
            pass

    async def _botao_modal_por_texto(self, textos: list[str]):
        if not self._page:
            return None
        await self._rolar_modal_para_baixo()
        try:
            modal = self._modal_locator()
            for texto in textos:
                seletor = f"button:has-text('{texto}')"
                try:
                    loc = modal.locator(seletor)
                    if await loc.count() == 0:
                        continue
                    botao = await self._primeiro_visivel(loc, limite=5)
                    if botao is None:
                        continue
                    try:
                        if await botao.is_disabled(timeout=300):
                            continue
                    except Exception:
                        pass
                    await self._rolar_modal_ate_botao(botao)
                    return botao
                except Exception:
                    continue
        except Exception:
            return None
        return None

    async def _rolar_modal_ate_botao(self, locator) -> None:
        try:
            await locator.evaluate(
                """
                (el) => {
                    const modal = el.closest('.jobs-easy-apply-modal, [role="dialog"], .artdeco-modal');
                    const body = modal?.querySelector('.jobs-easy-apply-modal__content, .artdeco-modal__content');
                    if (body) body.scrollTop = body.scrollHeight;
                    el.scrollIntoView({ block: 'center', inline: 'center' });
                }
                """
            )
            await self._pausa_visual(0.25)
        except Exception:
            pass

    def _preferencias_curriculo(self, tipo_vaga: str) -> list[str]:
        curriculos = self.perfil.get("curriculos", {})
        if isinstance(curriculos, dict):
            cfg = curriculos.get(tipo_vaga) or curriculos.get("geral")
            if isinstance(cfg, dict):
                termos = cfg.get("identificadores") or cfg.get("palavras_chave") or []
                if isinstance(termos, list) and termos:
                    return [str(t).lower() for t in termos]
            if isinstance(cfg, list) and cfg:
                return [str(t).lower() for t in cfg]

        if tipo_vaga == "ia":
            return ["analista de ia", "ia", "inteligencia artificial", "inteligência artificial", "ai"]
        if tipo_vaga == "infraestrutura":
            return ["analista de infraestrutura", "infraestrutura", "cloud", "devops", "redes"]
        return ["curriculo", "currículo", "resume"]

    async def _selecionar_curriculo(self, texto_vaga: str, tipo_vaga: str) -> None:
        if not self._page:
            return
        termos = self._preferencias_curriculo(tipo_vaga)
        try:
            modal = self._page.locator(".jobs-easy-apply-modal, [role='dialog'], .artdeco-modal").last
            opcoes = modal.locator(
                "label, "
                "div[role='radio'], "
                "li:has(input[type='radio']), "
                "div:has(input[type='radio'])"
            )
            count = min(await opcoes.count(), 25)
            melhor = None
            melhor_texto = ""
            melhor_pontos = 0
            for i in range(count):
                opcao = opcoes.nth(i)
                try:
                    if not await opcao.is_visible(timeout=500):
                        continue
                    texto = ((await opcao.inner_text()) or "").lower()
                    if not any(base in texto for base in ["curriculo", "currículo", "resume", ".pdf", ".doc"]):
                        continue
                    pontos = sum(1 for termo in termos if termo and termo in texto)
                    pontos += sum(1 for termo in TIPOS_DE_VAGA.get(tipo_vaga, []) if termo in texto_vaga.lower() and termo in texto)
                    if pontos > melhor_pontos:
                        melhor = opcao
                        melhor_texto = " ".join(texto.split())[:90]
                        melhor_pontos = pontos
                except Exception:
                    continue
            if melhor is not None and melhor_pontos > 0:
                # Não clicar se o rádio já está marcado
                ja_selecionado = False
                try:
                    radio = melhor.locator("input[type='radio']").first
                    if await radio.count() > 0:
                        ja_selecionado = await radio.is_checked()
                except Exception:
                    pass
                if not ja_selecionado:
                    await self._rolar_modal_ate_botao(melhor)
                    # Tenta clicar diretamente no input radio; fallback no elemento pai
                    clicou = False
                    try:
                        radio_direct = melhor.locator("input[type='radio']").first
                        if await radio_direct.count() > 0:
                            await radio_direct.dispatch_event("click")
                            clicou = True
                    except Exception:
                        pass
                    if not clicou:
                        clicou = await self._click_assistido(melhor, f"curriculo {tipo_vaga}", timeout=5000)
                    if clicou:
                        self.log(f"Curriculo escolhido para vaga {tipo_vaga}: {melhor_texto}")
        except Exception as e:
            self.log(f"AVISO: nao consegui escolher curriculo automaticamente: {e}")

    def _resposta_radio(self, ctx: str) -> Optional[str]:
        """Retorna 'yes' ou 'no' para perguntas de radio button baseado no contexto."""
        if any(k in ctx for k in ["sponsor", "patrocin", "immigration", "visa sponsorship", "visa transfer"]):
            return "no"
        if any(k in ctx for k in ["authorized", "autorizado", "lawfully", "eligible work", "work authorization"]):
            return "yes"
        if any(k in ctx for k in ["non-compete", "non-solicit"]):
            return "no"
        if any(k in ctx for k in ["ever worked for", "worked for", "previously employed", "affiliated compan"]):
            return "no"
        if any(k in ctx for k in ["ever interviewed", "interviewed with", "previously interviewed"]):
            return "no"
        if any(k in ctx for k in ["certify", "i certify", "acknowledge", "i agree", "i understand",
                                   "lie detector", "misdemeanor", "background investigation",
                                   "complete and accurate", "duly authorized"]):
            return "yes"
        return None

    async def _preencher_radio_buttons(self) -> None:
        if not self._page:
            return
        try:
            modal = self._modal_locator()
            radios = modal.locator("input[type='radio']")
            count = min(await radios.count(), 50)
            processados: set = set()
            for i in range(count):
                radio = radios.nth(i)
                try:
                    name = await radio.get_attribute("name") or f"_anon_{i}"
                    if name in processados:
                        continue
                    processados.add(name)
                    ctx = self._normalizar_texto(await radio.evaluate("""
                        (el) => {
                            const c = el.closest('fieldset') || el.closest('[role="group"]')
                                   || el.closest('.form-group') || el.closest('.field')
                                   || el.parentElement?.parentElement?.parentElement;
                            return (c?.innerText || el.parentElement?.parentElement?.innerText || '').slice(0, 600);
                        }
                    """))
                    resposta = self._resposta_radio(ctx)
                    if resposta is None:
                        continue
                    nome_escaped = name.replace("'", "\\'")
                    grupo = modal.locator(f"input[type='radio'][name='{nome_escaped}']")
                    g_count = min(await grupo.count(), 6)
                    for j in range(g_count):
                        r = grupo.nth(j)
                        try:
                            label_texto = self._normalizar_texto(await r.evaluate("""
                                (el) => {
                                    const id = el.id;
                                    if (id) {
                                        const lbl = document.querySelector(`label[for="${CSS.escape(id)}"]`);
                                        if (lbl) return lbl.innerText;
                                    }
                                    return el.closest('label')?.innerText || el.value || '';
                                }
                            """))
                            if resposta in label_texto:
                                try:
                                    if await r.is_checked(timeout=300):
                                        break
                                except Exception:
                                    pass
                                await self._click_assistido(r, f"radio '{label_texto}'", timeout=3000)
                                break
                        except Exception:
                            continue
                except Exception:
                    continue
        except Exception:
            pass

    async def _tem_erros_validacao(self) -> bool:
        if not self._page:
            return False
        try:
            modal = self._modal_locator()
            count = await modal.locator(
                ".artdeco-inline-feedback--error, "
                "[data-test-inline-feedback='error'], "
                ".error-message, p.error, span.error, "
                ".field-error, .validation-error, "
                "[class*='error']:not(script)"
            ).count()
            if count > 0:
                return True
            try:
                texto = (await modal.inner_text(timeout=2000) or "").lower()
            except Exception:
                texto = ""
            return any(t in texto for t in [
                "please enter a valid", "please make a selection",
                "este campo é obrigatório", "campo obrigatório",
                "required field", "invalid answer", "preencha este campo",
                "fill in this field", "this field is required",
                "por favor selecione", "selecione uma opcao",
            ])
        except Exception:
            return False

    async def _selecionar_autocomplete_cidade(self) -> None:
        if not self._page:
            return
        try:
            await asyncio.sleep(0.5)
            sugestoes = self._page.locator(
                ".basic-typeahead__triggered-content li, "
                ".artdeco-typeahead__results-list li"
            )
            count = await sugestoes.count()
            if count == 0:
                return
            cidade_norm = self._normalizar_texto(self.perfil.get("cidade", "brasilia"))
            prefer = self._normalizar_texto(self.perfil.get("cidade_autocomplete_prefer", "distrito federal"))
            preferido = None
            for i in range(min(count, 8)):
                item = sugestoes.nth(i)
                try:
                    texto = self._normalizar_texto(await item.inner_text())
                    if prefer in texto and cidade_norm in texto:
                        preferido = item
                        break
                    if preferido is None and cidade_norm in texto:
                        preferido = item
                except Exception:
                    continue
            if preferido is not None:
                await self._click_assistido(preferido, "sugestao de cidade", timeout=3000)
        except Exception:
            pass

    def _modal_locator(self):
        return self._page.locator(
            ".jobs-easy-apply-modal, "
            "div[role='dialog'][aria-modal='true'], "
            ".artdeco-modal--layer-default"
        ).last

    async def _preencher_campos_modal(self) -> None:
        if not self._page:
            return
        try:
            modal = self._modal_locator()
            campos = await modal.locator(
                "input:not([type='hidden']):not([type='submit']):not([type='button'])"
                ":not([type='radio']):not([type='checkbox']):not([type='file']), textarea"
            ).all()
        except Exception:
            return
        for campo in campos:
            try:
                valor = await campo.input_value()
                if valor:
                    continue
                placeholder = (await campo.get_attribute("placeholder")) or ""
                aria = (await campo.get_attribute("aria-label")) or ""
                name = (await campo.get_attribute("name")) or ""
                tipo = (await campo.get_attribute("type")) or ""
                contexto_visual = await self._contexto_do_campo(campo)
                ctx = self._normalizar_texto(f"{placeholder} {aria} {name} {contexto_visual}")
                resposta = self._resposta_para_contexto(ctx)
                if resposta:
                    await campo.fill(self._formatar_resposta_para_campo(resposta, tipo, ctx))
                    if any(k in ctx for k in ["cidade", "city", "location", "localizacao"]):
                        await self._selecionar_autocomplete_cidade()
                    continue
                if any(k in ctx for k in ["telefone", "phone", "celular"]):
                    tel = self.perfil.get("telefone")
                    if tel:
                        await campo.fill(str(tel))
                    continue
                if any(k in ctx for k in ["cidade", "city", "location", "localizacao"]):
                    await campo.fill(self.perfil.get("cidade", "Brasília"))
                    await self._selecionar_autocomplete_cidade()
                    continue
                # Pergunta de nível com opções numéricas (SAP/ERP) — responde 3 = Básico
                if any(k in ctx for k in ["sap", "erp", "benner", "alvo", "totvs", "protheus"]) and \
                        any(k in ctx for k in ["numero da opcao", "numero", "avancado", "intermediario", "basico", "opcao"]):
                    await campo.fill("3")
                    continue
                if any(k in ctx for k in ["anos", "years", "experiencia", "experience",
                                           "tempo", "how long", "quanto tempo", "trabalha com",
                                           "usa o", "usa a", "use the", "using"]):
                    tecnologias_fora = ["sap", "erp", "benner", "alvo", "totvs", "protheus",
                                        "rh", "financeiro", "fiscal", "controladoria"]
                    # Tecnologias de DEV que o usuário tem conhecimento básico
                    tecnologias_dev = ["node", "react", "angular", "vue", "java ", "php",
                                       "ruby", "golang", "go ", "kotlin", "swift", ".net"]
                    if any(k in ctx for k in tecnologias_fora):
                        continue  # Não preencher para techs totalmente fora do perfil
                    anos = self.perfil.get("anos_experiencia", 12)
                    if any(k in ctx for k in tecnologias_dev):
                        await campo.fill("1")  # Básico para dev fora do foco principal
                    else:
                        await campo.fill(str(anos))
                    continue
                if any(k in ctx for k in ["salario", "salary", "pretensao", "remuneration"]):
                    salario = self.perfil.get("pretensao_salarial")
                    if salario:
                        await campo.fill(str(salario))
                    continue
                if any(k in ctx for k in ["headline", "titulo profissional", "professional headline", "cargo atual", "job title"]):
                    await campo.fill(self.perfil.get("titulo_profissional", "Analista de TI | Infraestrutura | IA | Cloud | DevOps | Python"))
                    continue
                if any(k in ctx for k in ["summary", "sobre voce", "about you", "professional summary", "resumo profissional"]):
                    await campo.fill(self.perfil.get("resumo", ""))
                    continue
                if any(k in ctx for k in ["mensagem", "cover letter", "cover", "carta", "carta de apresentacao"]):
                    valor_atual = await campo.input_value() or ""
                    template_markers = ["bigdatacorp", "company name", "[company]", "estagiar na", "vejo na"]
                    eh_template = any(m in valor_atual.lower() for m in template_markers)
                    if not valor_atual or eh_template:
                        await campo.clear()
                        await campo.fill(self._texto_apresentacao())
                    continue
                if tipo in ("text", "email") and any(k in ctx for k in ["nome", "name"]):
                    await campo.fill(self.perfil.get("nome", "Seu Nome"))
                    continue
            except Exception:
                continue
        await self._preencher_selects()
        await self._preencher_dropdowns_customizados()
        await self._preencher_radio_buttons()

    async def _contexto_do_campo(self, campo) -> str:
        try:
            return await campo.evaluate(
                """
                (el) => {
                    const bits = [];
                    const id = el.getAttribute('id');
                    if (id) {
                        document.querySelectorAll(`label[for="${CSS.escape(id)}"]`).forEach(label => {
                            bits.push(label.innerText || label.textContent || '');
                        });
                    }
                    let node = el;
                    for (let i = 0; i < 4 && node; i++) {
                        bits.push(node.innerText || node.textContent || '');
                        node = node.parentElement;
                    }
                    return bits.join(' ').slice(0, 1200);
                }
                """
            )
        except Exception:
            return ""

    def _valor_perfil(self, *chaves, padrao=""):
        for chave in chaves:
            valor = self.perfil.get(chave)
            if valor not in (None, ""):
                return valor
        return padrao

    def _formatar_resposta_para_campo(self, resposta, tipo: str, ctx: str) -> str:
        if any(k in ctx for k in ["pretensao", "pretencao", "salary", "remuneration", "remuneracao"]):
            try:
                valor = float(str(resposta).replace(",", "."))
                return f"{valor:.1f}"
            except Exception:
                return str(resposta)
        return str(resposta)

    def _resposta_para_contexto(self, ctx: str):
        if not ctx:
            return None
        if any(k in ctx for k in ["pretensao", "pretencao", "pretensao salarial", "salary", "remuneration", "remuneracao"]):
            if any(k in ctx for k in ["usd", "dollar", "dolar", "monthly salary expectations"]):
                return self._valor_perfil("pretensao_salarial_usd", "pretensao_salarial_usd_mensal", padrao=1600)
            if any(k in ctx for k in ["pj", "cooperado", "contractor", "prestador"]):
                return self._valor_perfil("pretensao_salarial_pj", padrao=8500)
            return self._valor_perfil("pretensao_salarial_clt", "pretensao_salarial", padrao=8000)
        if any(k in ctx for k in ["ingles", "english", "spoken english"]):
            if any(k in ctx for k in ["c1", "c2", "avancado", "advanced", "conversation", "conversacao", "spoken"]):
                return "Yes" if self.perfil.get("ingles_avancado", True) else "No"
            return self._valor_perfil("nivel_ingles", padrao="Advanced")
        if any(k in ctx for k in ["portfolio", "site", "website", "url"]):
            return self._valor_perfil("site", "portfolio", padrao="https://www.lstechia.com.br/")
        if "linkedin" in ctx:
            return self._valor_perfil("linkedin", padrao=LINKEDIN_PERFIL_URL)
        if "github" in ctx:
            return self._valor_perfil("github", padrao="")
        if any(k in ctx for k in ["disponibilidade", "availability", "inicio", "start date"]):
            return self._valor_perfil("disponibilidade", padrao="Imediata")
        if any(k in ctx for k in ["modelo de trabalho", "work model", "remoto", "remote", "hibrido", "hybrid"]):
            return self._valor_perfil("modelo_trabalho", padrao="Remoto, hibrido ou presencial")
        if any(k in ctx for k in ["headline", "titulo profissional", "professional headline", "cargo atual", "job title"]):
            return self._valor_perfil("titulo_profissional", padrao="Analista de TI | Infraestrutura | IA | Cloud | DevOps | Python")
        if any(k in ctx for k in ["anos", "years", "experiencia", "experience",
                                    "tempo", "how long", "quanto tempo", "trabalha com"]):
            tecnologias_fora = ["sap", "erp", "benner", "alvo", "totvs", "protheus",
                                 "rh", "financeiro", "fiscal", "controladoria"]
            tecnologias_dev = ["node", "react", "angular", "vue", "java ", "php",
                                "ruby", "golang", "go ", "kotlin", "swift", ".net"]
            if any(k in ctx for k in tecnologias_fora):
                return None
            if any(k in ctx for k in tecnologias_dev):
                return "1"
            return self._valor_perfil("anos_experiencia", padrao=12)
        if any(k in ctx for k in ["cidade", "city", "localizacao", "location"]):
            return self._valor_perfil("cidade", padrao="Brasília")
        if any(k in ctx for k in ["street address", "endereco", "address line 1", "address 1", "rua", "logradouro"]):
            if not any(k in ctx for k in ["2", "second", "line 2", "address 2"]):
                return self._valor_perfil("endereco_rua", padrao="SQN 302 Bloco E Apt 301")
        if any(k in ctx for k in ["state", "estado", "province", "provincia"]):
            return self._valor_perfil("endereco_estado", padrao="DF")
        if any(k in ctx for k in ["zip", "postal", "cep", "codigo postal"]):
            return self._valor_perfil("endereco_cep", padrao="70737-050")
        if any(k in ctx for k in ["country", "pais", "país"]):
            return self._valor_perfil("endereco_pais", padrao="Brazil")
        return None

    def _opcao_para_contexto(self, ctx: str, opcoes: list[str]) -> Optional[str]:
        if not opcoes:
            return None
        ignorar = (
            "candidatura simplificada",
            "easy apply",
            "vagas",
            "mais recentes",
            "mais relevante",
            "remoto",
            "presencial",
            "hibrido",
            "filtro",
        )
        normalizadas = [
            (opcao, self._normalizar_texto(opcao))
            for opcao in opcoes
            if self._normalizar_texto(opcao)
            and not any(bloqueado in self._normalizar_texto(opcao) for bloqueado in ignorar)
        ]
        if not normalizadas:
            return None
        exige_preferida = False
        if self._pergunta_sobre_tecnologia_fora_do_perfil(ctx):
            preferidas = ["nao", "não", "no", "no tengo", "no possuo"]
            exige_preferida = True
        elif any(k in ctx for k in ["non-compete", "non-solicit", "nao compete", "nao concorr"]):
            preferidas = ["no", "nao", "não"]
            exige_preferida = True
        elif any(k in ctx for k in ["sponsor", "patrocin", "immigration", "visa sponsorship"]):
            preferidas = ["no", "nao", "não"]
            exige_preferida = True
        elif any(k in ctx for k in ["worked for", "ever worked", "previously employed", "worked at"]):
            preferidas = ["no", "nao", "não"]
            exige_preferida = True
        elif any(k in ctx for k in ["ma conectividade", "ma internet", "problema de internet",
                                        "conexao ruim", "dificuldade de conexao"]):
            preferidas = ["nao", "não", "no"]
            exige_preferida = True
        elif any(k in ctx for k in ["rh", "recursos humanos", "financeiro", "fiscal",
                                        "controladoria", "contabil", "folha de pagamento",
                                        "sap business", "s/4hana", "s4hana", "business one"]):
            preferidas = ["nao", "não", "no"]
            exige_preferida = True
        elif any(k in ctx for k in ["possui", "experiencia", "experience", "tem conhecimento", "conhecimento", "atuou", "trabalhou"]):
            preferidas = ["sim", "yes", "si", "tenho", "possuo"]
            exige_preferida = True
        elif any(k in ctx for k in ["ingles", "english", "spoken english", "autorizado", "authorized", "eligible", "lawfully"]):
            preferidas = ["yes", "sim", "si", "avancado", "advanced", "c1", "c2"]
            exige_preferida = True
        elif any(k in ctx for k in ["nivel", "level"]):
            preferidas = ["avancado", "advanced", "senior", "pleno"]
            exige_preferida = True
        else:
            preferidas = ["yes", "sim", "aceito", "concordo"]
        for alvo in preferidas:
            for original, normalizada in normalizadas:
                if alvo in normalizada:
                    return original
        if exige_preferida:
            return None
        for original, normalizada in normalizadas:
            if normalizada not in ("selecionar opcao", "selecciona una opcion", "select an option", "selecione", "select"):
                return original
        return None

    def _pergunta_sobre_tecnologia_fora_do_perfil(self, ctx: str) -> bool:
        if not any(k in ctx for k in ["possui", "experiencia", "experience", "conhecimento", "atuou", "trabalhou"]):
            return False
        tecnologias_perfil = " ".join(str(t) for t in self.perfil.get("tecnologias", []))
        tecnologias_perfil = self._normalizar_texto(tecnologias_perfil)
        for termo in TECNOLOGIAS_ESPECIFICAS:
            termo_norm = self._normalizar_texto(termo)
            if termo_norm in ctx and termo_norm not in tecnologias_perfil:
                return True
        return False

    async def _preencher_selects(self) -> None:
        try:
            modal = self._modal_locator()
            selects = await modal.locator("select").all()
            if selects:
                self.log(f"Preenchendo {len(selects)} select(s) no modal...")
            for s in selects:
                try:
                    # Pula se já tem valor selecionado
                    atual = await s.input_value()
                    if atual and atual not in ("", "null", "undefined"):
                        continue
                    # Usa JS para ler opções — mais confiável que locator("option")
                    opcoes_raw = await s.evaluate(
                        "el => Array.from(el.options).map(o => o.text.trim())"
                    )
                    opcoes = [o for o in opcoes_raw if o]
                    if not opcoes:
                        continue
                    ctx = self._normalizar_texto(await self._contexto_do_campo(s))
                    preferida = self._opcao_para_contexto(ctx, opcoes)
                    if preferida:
                        self.log(f"Select [{ctx[:40]}] → '{preferida}'")
                        try:
                            await s.select_option(label=preferida)
                        except Exception:
                            await s.evaluate(
                                """(el, val) => {
                                    const opt = Array.from(el.options).find(o => o.text.trim() === val);
                                    if (opt) { el.value = opt.value; el.dispatchEvent(new Event('change', {bubbles:true})); }
                                }""",
                                preferida
                            )
                    else:
                        self.log(f"Select [{ctx[:40]}] sem opcao compativel. Opcoes: {opcoes[:5]}")
                except Exception:
                    continue
        except Exception:
            pass

    async def _preencher_dropdowns_customizados(self) -> None:
        if not self._page:
            return
        try:
            modal = self._modal_locator()
            botoes = modal.locator("button, [role='button']")
            count = min(await botoes.count(), 30)
            for i in range(count):
                botao = botoes.nth(i)
                try:
                    if not await botao.is_visible(timeout=200):
                        continue
                    texto_botao = await botao.inner_text() or ""
                    texto_botao_norm = self._normalizar_texto(texto_botao)
                    aria_botao = await botao.get_attribute("aria-label") or ""
                    aria_botao_norm = self._normalizar_texto(aria_botao)
                    placeholder_dropdown = (
                        "selecionar opcao",
                        "selecciona una opcion",
                        "select an option",
                        "selecione uma opcao",
                    )
                    if not any(p in texto_botao_norm for p in placeholder_dropdown):
                        continue
                    contexto_botao = await self._contexto_do_campo(botao)
                    rotulo = self._normalizar_texto(
                        f"{texto_botao} "
                        f"{aria_botao} "
                        f"{await botao.get_attribute('aria-haspopup') or ''} "
                        f"{contexto_botao}"
                    )
                    if not any(k in rotulo for k in placeholder_dropdown):
                        continue
                    if any(k in aria_botao_norm for k in ["email", "e-mail", "codigo do pais", "country code"]):
                        continue
                    ctx = self._normalizar_texto(contexto_botao)
                    await self._click_assistido(botao, "dropdown de pergunta", timeout=4000)
                    await asyncio.sleep(0.8)
                    opcoes = self._page.locator(
                        "[role='listbox'] [role='option'], "
                        ".artdeco-dropdown__content--is-open [role='option'], "
                        ".artdeco-dropdown__content--is-open .artdeco-dropdown__item, "
                        "ul.artdeco-dropdown__content li[role='option'], "
                        "[role='option']:visible, "
                        "li[data-test-form-builder-select-options], "
                        ".fb-form-element__dropdown-option, "
                        ".artdeco-typeahead__results-list li"
                    )
                    total = min(await opcoes.count(), 30)
                    textos = []
                    itens = []
                    for j in range(total):
                        item = opcoes.nth(j)
                        try:
                            if await item.is_visible(timeout=300):
                                texto = await item.inner_text()
                                if texto:
                                    textos.append(texto)
                                    itens.append(item)
                        except Exception:
                            continue
                    escolhida = self._opcao_para_contexto(ctx, textos)
                    if escolhida:
                        for texto, item in zip(textos, itens):
                            if texto == escolhida:
                                await self._click_assistido(item, f"opcao {escolhida}", timeout=4000)
                                break
                except Exception:
                    continue
        except Exception:
            pass

    def _texto_apresentacao(self) -> str:
        return (
            f"Ola, meu nome e {self.perfil.get('nome', 'Seu Nome')}. "
            "Atuo com infraestrutura de TI, sistemas, redes, servidores Windows e Linux, "
            "virtualizacao, backup, cloud AWS/Azure, ciberseguranca, automacao em Python "
            "e inteligencia artificial. Pela LS TECHIA, desenvolvo sistemas web, CRMs, "
            "agentes de IA, automacoes e solucoes de infraestrutura em producao. "
            "Tenho forte interesse na oportunidade por estar alinhada a minha trajetoria."
        )

    async def _fechar_modal(self) -> None:
        if not self._page:
            return
        for _ in range(5):
            clicou = False
            try:
                descartar = self._page.locator(
                    "button:has-text('Descartar'), "
                    "button:has-text('Discard'), "
                    "button:has-text('Sair mesmo assim'), "
                    "button:has-text('Leave'), "
                    "button:has-text('No guardar'), "
                    "button:has-text('Descartar solicitud')"
                )
                botao_descartar = await self._primeiro_visivel(descartar, limite=8)
                if botao_descartar is not None:
                    await self._click_assistido(botao_descartar, "botao Descartar", timeout=3000)
                    await asyncio.sleep(0.5)
                    clicou = True
                    continue

                fechar = self._page.locator(
                    ".jobs-easy-apply-modal button[aria-label='Fechar'], "
                    ".jobs-easy-apply-modal button[aria-label='Dismiss'], "
                    ".jobs-easy-apply-modal button[aria-label='Close'], "
                    ".artdeco-modal button[aria-label='Fechar'], "
                    ".artdeco-modal button[aria-label='Dismiss'], "
                    ".artdeco-modal button[aria-label='Close'], "
                    "[role='dialog'] button[aria-label='Fechar'], "
                    "[role='dialog'] button[aria-label='Dismiss'], "
                    "[role='dialog'] button[aria-label='Close']"
                )
                botao_fechar = await self._primeiro_visivel(fechar, limite=12)
                if botao_fechar is not None:
                    await self._click_assistido(botao_fechar, "X do modal", timeout=3000)
                    await asyncio.sleep(0.5)
                    clicou = True
                    continue
            except Exception:
                pass
            if not clicou:
                break

    def solicitar_parada(self) -> None:
        self.estado.parando = True
        self.stop_event.set()
        self.log("DESLIGAR recebido. Aguardando finalizacao limpa...")


_agente_atual: Optional[AgenteLinkedIn] = None


def obter_agente(log_callback: Callable[[str], None] = print) -> AgenteLinkedIn:
    global _agente_atual
    if _agente_atual is None or not _agente_atual.estado.rodando:
        _agente_atual = AgenteLinkedIn(log_callback=log_callback)
    return _agente_atual


async def executar_agente(log_callback: Callable[[str], None] = print) -> None:
    agente = obter_agente(log_callback)
    await agente.executar()


def parar_agente() -> None:
    if _agente_atual is not None:
        _agente_atual.solicitar_parada()


def snapshot_estado() -> dict:
    if _agente_atual is None:
        return {
            "rodando": False,
            "parando": False,
            "candidaturas_enviadas": 0,
            "vagas_analisadas": 0,
            "erro_atual": None,
            "iniciado_em": None,
            "ultima_atividade": None,
            "pesquisa_atual": None,
            "total_aplicadas_persistente": 0,
        }
    return _agente_atual.estado.snapshot()
