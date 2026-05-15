"""
LS.IA Agent 01 - API FastAPI
Versao: 2.0
"""

from __future__ import annotations

import asyncio
from collections import deque
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, Body
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from agente_linkedin import AgenteLinkedIn, carregar_config_runtime, salvar_config_runtime, snapshot_estado


LOGS_MAX = 500
logs: deque = deque(maxlen=LOGS_MAX)
agente: Optional[AgenteLinkedIn] = None
task_agente: Optional[asyncio.Task] = None


def add_log(msg: str) -> None:
    s = str(msg)
    print(s, flush=True)
    logs.append(s)


def estado_atual() -> dict:
    if agente is not None:
        return agente.estado.snapshot()
    return snapshot_estado()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    add_log("Servidor LS.IA Agent 01 inicializado.")
    yield
    global agente, task_agente
    if agente is not None:
        agente.solicitar_parada()
    if task_agente is not None and not task_agente.done():
        task_agente.cancel()
        try:
            await asyncio.wait_for(task_agente, timeout=15)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass
    add_log("Servidor encerrado.")


app = FastAPI(title="LS.IA Agent 01", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
def home():
    return FileResponse("static/index.html")


@app.post("/ligar")
async def ligar():
    global agente, task_agente

    if task_agente is not None and not task_agente.done():
        return {"status": "Agente ja esta rodando.", "ok": False}

    logs.clear()
    add_log("Comando: LIGAR.")

    agente = AgenteLinkedIn(log_callback=add_log)
    task_agente = asyncio.create_task(agente.executar(), name="agente-01")

    return {"status": "Agente ligado.", "ok": True}


@app.post("/desligar")
async def desligar():
    global agente, task_agente
    if agente is None or task_agente is None or task_agente.done():
        return {"status": "Agente ja estava parado.", "ok": False}

    agente.solicitar_parada()
    add_log("Comando: DESLIGAR (parada limpa).")

    try:
        await asyncio.wait_for(asyncio.shield(task_agente), timeout=25)
    except asyncio.TimeoutError:
        add_log("Parada limpa estourou 25s. Cancelando task forcadamente...")
        task_agente.cancel()
        try:
            await asyncio.wait_for(task_agente, timeout=10)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass

    return {"status": "Agente desligado.", "ok": True}


@app.post("/forcar-parada")
async def forcar_parada():
    global agente, task_agente
    if task_agente is None or task_agente.done():
        return {"status": "Nao ha agente rodando.", "ok": False}

    add_log("Comando: FORCAR PARADA (cancelamento bruto).")
    if agente is not None:
        agente.solicitar_parada()
    task_agente.cancel()
    try:
        await asyncio.wait_for(task_agente, timeout=10)
    except (asyncio.CancelledError, asyncio.TimeoutError):
        pass
    return {"status": "Agente forcadamente cancelado.", "ok": True}


@app.get("/logs")
def get_logs():
    return {"logs": list(logs)}


@app.get("/status")
def get_status():
    rodando = task_agente is not None and not task_agente.done()
    return {
        "rodando": rodando,
        "tem_agente": agente is not None,
        "estado": estado_atual(),
    }


@app.get("/stats")
def get_stats():
    estado = estado_atual()
    return JSONResponse(
        {
            "candidaturas_enviadas": estado.get("candidaturas_enviadas", 0),
            "vagas_analisadas": estado.get("vagas_analisadas", 0),
            "total_aplicadas_persistente": estado.get("total_aplicadas_persistente", 0),
            "pesquisa_atual": estado.get("pesquisa_atual"),
            "ultima_atividade": estado.get("ultima_atividade"),
            "erro_atual": estado.get("erro_atual"),
            "iniciado_em": estado.get("iniciado_em"),
        }
    )


@app.get("/config")
def get_config():
    return carregar_config_runtime()


@app.post("/config")
def post_config(payload: dict = Body(...)):
    if task_agente is not None and not task_agente.done():
        return JSONResponse(
            {"ok": False, "status": "Desligue o agente antes de alterar controles de custo."},
            status_code=409,
        )
    config = salvar_config_runtime(payload)
    add_log("Controles de custo/IA atualizados no painel.")
    return {"ok": True, "config": config}
