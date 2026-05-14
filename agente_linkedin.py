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

CHROME_USER_DATA_DIR = os.getenv("CHROME_USER_DATA_DIR", str(BASE_DIR / "chrome_agent_profile_real"))
CHROME_PROFILE_NAME = os.getenv("CHROME_PROFILE_NAME", "Default")

LINKEDIN_PERFIL_URL = os.getenv("LINKEDIN_PERFIL_URL", "https://www.linkedin.com/in/")
LINKEDIN_JOBS_URL = "https://www.linkedin.com/jobs/"

CDP_HOST = "127.0.0.1"
CDP_PORT = int(os.getenv("CDP_PORT", "9222"))
CDP_URL = f"http://{CDP_HOST}:{CDP_PORT}"

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434/api/generate")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3.2:3b")

PESQUISAS_PADRAO = [
    "engenheiro de inteligencia artificial",
    "engenheiro de IA",
    "AI engineer",
    "infraestrutura de TI",
    "analista de infraestrutura cloud",
    "cloud engineer",
    "AWS cloud engineer",
    "Azure cloud engineer",
    "devops",
    "python developer",
    "cyber security",
    "seguranca da informacao",
]

AUTO_ENVIAR_CANDIDATURA = True
MODO_ASSISTIDO = True  # destaque visual + traz Chrome pra frente
DELAY_VISUAL_ASSISTIDO_S = 1.2
CURSOR_VISUAL_ID = "lsia-agent-cursor"
CURSOR_PULSE_ID = "lsia-agent-cursor-pulse"
MAX_VAGAS_POR_CICLO = 25
MAX_ETAPAS_CANDIDATURA = 10
ESPERA_ENTRE_CICLOS_S = (45, 90)
ESPERA_ENTRE_VAGAS_S = (6, 14)
ESPERA_ENTRE_BUSCAS_S = (8, 18)


def _agora() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _log_arquivo(msg: str) -> None:
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"[{_agora()}] {msg}\n")
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
        passos = max(1, int(total))
        for _ in range(passos):
            if self.stop_event.is_set():
                return
            await asyncio.sleep(1)

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
            await locator.click(timeout=timeout)
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
                link = await vaga.get_attribute("href")
                if not link:
                    continue
                chave = link.split("?")[0]
                if chave in self.estado.vagas_aplicadas:
                    continue
                if not await self._click_assistido(vaga, "card da vaga", timeout=8000):
                    continue
                await self._sleep_jitter((3, 6))
                await self._fechar_popups()
                self.estado.vagas_analisadas += 1
                texto = await self._page.locator("body").inner_text()
                if not self._tem_candidatura_simplificada(texto):
                    continue
                if not await self._ia_diz_compativel(texto):
                    continue
                self.log("Vaga compativel. Iniciando candidatura...")
                enviou = await self._tentar_candidatar()
                if enviou:
                    self.estado.candidaturas_enviadas += 1
                    self.estado.vagas_aplicadas.add(chave)
                    self._salvar_vagas_aplicadas()
                    self.log(f"Candidatura ENVIADA. Total nesta sessao: {self.estado.candidaturas_enviadas}")
                await self._sleep_jitter(ESPERA_ENTRE_VAGAS_S)
            except Exception as e:
                self.log(f"AVISO: erro ao processar card: {e}")

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

    async def _ia_diz_compativel(self, texto_vaga: str) -> bool:
        prompt = (
            "Voce e um filtro de match de curriculo. Responda APENAS no formato:\n"
            "DECISAO: COMPATIVEL\nMOTIVO: <frase curta>\n"
            "ou\n"
            "DECISAO: NAO_COMPATIVEL\nMOTIVO: <frase curta>\n\n"
            f"PERFIL:\n{json.dumps(self.perfil, ensure_ascii=False)}\n\n"
            f"VAGA:\n{texto_vaga[:4500]}\n"
        )
        try:
            async with httpx.AsyncClient(timeout=120) as client:
                r = await client.post(
                    OLLAMA_URL,
                    json={"model": OLLAMA_MODEL, "prompt": prompt, "stream": False},
                )
                resposta = r.json().get("response", "")
        except Exception as e:
            self.log(f"AVISO: Ollama indisponivel ({e}). Heuristica aplicada.")
            return self._fallback_heuristico(texto_vaga)
        decisao = resposta.upper()
        if "NAO_COMPATIVEL" in decisao or "NAO COMPATIVEL" in decisao:
            return False
        return "COMPATIVEL" in decisao

    def _fallback_heuristico(self, texto_vaga: str) -> bool:
        t = texto_vaga.lower()
        palavras = [
            "infraestrutura", "infrastructure", "cloud", "aws", "azure",
            "devops", "python", "linux", "windows server", "cyber",
            "seguranca", "security", "inteligencia artificial", "ai engineer",
            "machine learning", "redes", "network",
        ]
        return sum(1 for p in palavras if p in t) >= 2

    async def _tentar_candidatar(self) -> bool:
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
            await self._sleep_jitter((2, 4))
            for _etapa in range(1, MAX_ETAPAS_CANDIDATURA + 1):
                if self.stop_event.is_set():
                    return False
                await self._preencher_campos_modal()
                await asyncio.sleep(1)
                texto = await self._page.locator("body").inner_text()
                if "Enviar candidatura" in texto or "Submit application" in texto:
                    if not AUTO_ENVIAR_CANDIDATURA:
                        await self._fechar_modal()
                        return False
                    enviar = self._page.locator(
                        "button:has-text('Enviar candidatura'), "
                        "button:has-text('Submit application')"
                    )
                    enviar_visivel = await self._primeiro_visivel(enviar)
                    if enviar_visivel is not None:
                        if not await self._click_assistido(enviar_visivel, "botao Enviar candidatura", timeout=8000):
                            return False
                        await self._sleep_jitter((3, 5))
                        await self._fechar_modal()
                        return True
                revisar = self._page.locator(
                    "button:has-text('Revisar'), button:has-text('Review')"
                )
                revisar_visivel = await self._primeiro_visivel(revisar)
                if revisar_visivel is not None:
                    if not await self._click_assistido(revisar_visivel, "botao Revisar", timeout=8000):
                        return False
                    await self._sleep_jitter((2, 4))
                    continue
                proximo = self._page.locator(
                    "button:has-text('Avancar'), "
                    "button:has-text('Proximo'), "
                    "button:has-text('Next')"
                )
                proximo_visivel = await self._primeiro_visivel(proximo)
                if proximo_visivel is not None:
                    if not await self._click_assistido(proximo_visivel, "botao Proximo", timeout=8000):
                        return False
                    await self._sleep_jitter((2, 4))
                    continue
                await self._fechar_modal()
                return False
            await self._fechar_modal()
            return False
        except Exception as e:
            self.log(f"AVISO: erro durante candidatura: {e}")
            await self._fechar_modal()
            return False

    async def _preencher_campos_modal(self) -> None:
        if not self._page:
            return
        campos = await self._page.locator(
            "input:not([type='hidden']):not([type='submit']):not([type='button']), textarea"
        ).all()
        for campo in campos:
            try:
                valor = await campo.input_value()
                if valor:
                    continue
                placeholder = (await campo.get_attribute("placeholder")) or ""
                aria = (await campo.get_attribute("aria-label")) or ""
                name = (await campo.get_attribute("name")) or ""
                tipo = (await campo.get_attribute("type")) or ""
                ctx = f"{placeholder} {aria} {name}".lower()
                if any(k in ctx for k in ["telefone", "phone", "celular"]):
                    tel = self.perfil.get("telefone")
                    if tel:
                        await campo.fill(str(tel))
                    continue
                if any(k in ctx for k in ["cidade", "city"]):
                    await campo.fill(self.perfil.get("cidade", "Brasilia"))
                    continue
                if any(k in ctx for k in ["anos", "years", "experiencia", "experience"]):
                    await campo.fill(str(self.perfil.get("anos_experiencia", 12)))
                    continue
                if any(k in ctx for k in ["salario", "salary", "pretensao", "remuneration"]):
                    salario = self.perfil.get("pretensao_salarial")
                    if salario:
                        await campo.fill(str(salario))
                    continue
                if any(k in ctx for k in ["mensagem", "cover", "resumo", "carta"]):
                    await campo.fill(self._texto_apresentacao())
                    continue
                if tipo in ("text", "email") and any(k in ctx for k in ["nome", "name"]):
                    await campo.fill(self.perfil.get("nome", "Seu Nome"))
                    continue
            except Exception:
                continue
        try:
            selects = await self._page.locator("select").all()
            for s in selects:
                try:
                    opcoes = await s.locator("option").all_text_contents()
                    preferida = None
                    for opt in opcoes:
                        if opt.strip().lower() in ("sim", "yes"):
                            preferida = opt
                            break
                    if preferida:
                        await s.select_option(label=preferida)
                except Exception:
                    continue
        except Exception:
            pass

    def _texto_apresentacao(self) -> str:
        return (
            f"Ola, meu nome e {self.perfil.get('nome', 'Seu Nome')}. "
            "Atuo com infraestrutura de TI, redes, servidores Windows e Linux, "
            "virtualizacao, cloud (AWS/Azure), cyber seguranca, automacao em Python "
            "e inteligencia artificial. Tenho forte interesse na oportunidade por "
            "estar diretamente alinhada a minha trajetoria."
        )

    async def _fechar_modal(self) -> None:
        if not self._page:
            return
        try:
            fechar = self._page.locator(
                "button[aria-label='Fechar'], "
                "button[aria-label='Dismiss'], "
                "button[aria-label='Close']"
            )
            if await fechar.count() > 0:
                await fechar.first.click()
                await asyncio.sleep(0.6)
            descartar = self._page.locator(
                "button:has-text('Descartar'), "
                "button:has-text('Discard'), "
                "button:has-text('Sair mesmo assim')"
            )
            if await descartar.count() > 0:
                await descartar.first.click()
                await asyncio.sleep(0.6)
        except Exception:
            pass

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
