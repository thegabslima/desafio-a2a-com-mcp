"""Agente Central de Salas: servidor A2A (JSON-RPC) por fora, host MCP por dentro.

Sem LLM: o pedido chega em formato fixo e o agente traduz protocolo. Regra de
sala (conflito, politica, alternativas) e decisao do servidor MCP.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import sys
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from cliente_mcp import ClienteMCP, ErroMCP

PORTA = int(os.environ.get("AGENTE_PORT", "7300"))
URL_PUBLICA = os.environ.get("AGENTE_URL", f"http://localhost:{PORTA}")
URL_MCP = os.environ.get("MCP_URL", "http://localhost:7301/mcp")
TOOL_RESERVA = "reservar_sala"
URI_POLITICA = "politica://uso"

TERMINAIS = {"TASK_STATE_COMPLETED", "TASK_STATE_CANCELED", "TASK_STATE_FAILED", "TASK_STATE_REJECTED"}
PEDIDO = re.compile(r"^reservar\s+sala=(\S+)\s+inicio=(\S+)\s+fim=(\S+)\s+responsavel=(.+)$")
ESCOLHA = re.compile(r"^escolha=(\S+)$")

# A2A: codigos de erro do binding JSON-RPC
TASK_NOT_FOUND, UNSUPPORTED_OPERATION = -32001, -32004
METHOD_NOT_FOUND, INVALID_PARAMS = -32601, -32602


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def agora() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Pausa:
    """O que a ponte guarda de um input_required. Nunca sai do processo do agente."""

    argumentos: dict[str, Any]
    chave: str            # chave atribuida pelo servidor em inputRequests
    request_state: str    # opaco: guardado e ecoado, nunca aberto
    opcoes: list[str]     # enum da elicitation, na ordem recebida


@dataclass
class Task:
    id: str
    context_id: str
    trace_id: str
    estado: str = "TASK_STATE_SUBMITTED"
    mensagem: dict[str, Any] | None = None
    history: list[dict[str, Any]] = field(default_factory=list)
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    pausa: Pausa | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def mudar(self, estado: str, texto: str | None = None) -> None:
        if self.estado in TERMINAIS:
            raise RuntimeError(f"task {self.id} ja esta em {self.estado}")
        self.estado = estado
        self.mensagem = None
        if texto is not None:
            self.mensagem = {
                "messageId": f"msg-{secrets.token_hex(6)}",
                "role": "ROLE_AGENT",
                "parts": [{"text": texto}],
                "taskId": self.id,
                "contextId": self.context_id,
            }
            self.history.append(self.mensagem)
        log(f"[a2a] task={self.id} estado={estado}")

    def json(self) -> dict[str, Any]:
        status: dict[str, Any] = {"state": self.estado, "timestamp": agora()}
        if self.mensagem:
            status["message"] = self.mensagem
        return {"id": self.id, "contextId": self.context_id, "status": status,
                "history": self.history, "artifacts": self.artifacts}


TASKS: dict[str, Task] = {}
mcp = ClienteMCP(URL_MCP)


class ErroA2A(Exception):
    def __init__(self, codigo: int, mensagem: str):
        super().__init__(mensagem)
        self.codigo = codigo


# ---------------------------------------------------------------- traducao MCP -> Task


def texto_de(resultado: dict[str, Any]) -> str:
    return " ".join(p.get("text", "") for p in resultado.get("content", []) if p.get("type", "text") == "text")


def aplicar_resultado(task: Task, resultado: dict[str, Any], argumentos: dict[str, Any], politica: str) -> None:
    """A ponte: traduz o resultado do tools/call no estado da Task."""
    if resultado.get("resultType") == "input_required":
        # input_required do MCP vira TASK_STATE_INPUT_REQUIRED do A2A.
        chave, pedido = next(iter(resultado["inputRequests"].items()))
        campo = pedido["params"]["requestedSchema"]["properties"]["sala"]
        opcoes = campo.get("enum") or [campo["const"]]
        task.pausa = Pausa(argumentos, chave, resultado["requestState"], opcoes)
        task.mudar("TASK_STATE_INPUT_REQUIRED", "alternativas: " + ", ".join(opcoes))
        return

    task.pausa = None
    if resultado.get("isError"):
        task.mudar("TASK_STATE_FAILED", texto_de(resultado))
        return
    dados = resultado.get("structuredContent") or {}
    if not dados.get("reservado"):
        task.mudar("TASK_STATE_CANCELED", f"Reserva nao realizada: {dados.get('motivo') or 'recusado'}.")
        return
    reserva = {k: dados.get(k) for k in ("reserva", "sala", "inicio", "fim", "responsavel")}
    reserva["politica"] = politica
    task.artifacts.append({
        "artifactId": f"art-{secrets.token_hex(6)}",
        "name": "reserva",
        "parts": [{"text": json.dumps(reserva, ensure_ascii=False)}],
    })
    task.mudar("TASK_STATE_COMPLETED", f"Reserva {reserva['reserva']} confirmada na {reserva['sala']}.")


async def politica_vigente(trace_id: str) -> str:
    texto = await mcp.ler_resource(URI_POLITICA, trace_id)
    return texto.splitlines()[0].split(":", 1)[1].strip()


async def iniciar(task: Task, texto: str) -> None:
    casado = PEDIDO.match(texto.strip())
    if not casado:
        task.mudar("TASK_STATE_FAILED",
                   "Pedido invalido: use reservar sala=<id> inicio=<iso8601> fim=<iso8601> responsavel=<nome>")
        return
    sala, inicio, fim, responsavel = casado.groups()
    argumentos = {"sala": sala, "inicio": inicio, "fim": fim, "responsavel": responsavel.strip()}

    tools = {t["name"] for t in await mcp.listar_tools(task.trace_id)}  # descoberta em runtime
    if TOOL_RESERVA not in tools:
        task.mudar("TASK_STATE_FAILED", f"O servidor MCP nao oferece a tool {TOOL_RESERVA}")
        return
    politica = await politica_vigente(task.trace_id)
    resultado = await mcp.chamar_tool(TOOL_RESERVA, argumentos, task.trace_id)
    aplicar_resultado(task, resultado, argumentos, politica)


async def retomar(task: Task, texto: str) -> None:
    pausa = task.pausa
    assert pausa is not None
    casado = ESCOLHA.match(texto.strip())
    valor = casado.group(1) if casado else None
    if valor == "recusar":
        resposta = {"action": "decline"}
    elif valor in pausa.opcoes:
        resposta = {"action": "accept", "content": {"sala": valor}}
    else:
        # Fora do enum: a Task continua pausada e a lista e repetida.
        task.mudar("TASK_STATE_INPUT_REQUIRED", "alternativas: " + ", ".join(pausa.opcoes))
        return

    task.mudar("TASK_STATE_WORKING")
    politica = await politica_vigente(task.trace_id)
    # Retry do tools/call original: id novo, mesma chave, requestState ecoado sem modificacao.
    resultado = await mcp.chamar_tool(
        TOOL_RESERVA, pausa.argumentos, task.trace_id,
        input_responses={pausa.chave: resposta}, request_state=pausa.request_state,
    )
    aplicar_resultado(task, resultado, pausa.argumentos, politica)


# ---------------------------------------------------------------- A2A JSON-RPC


def trace_id_de(request: Request) -> str | None:
    partes = (request.headers.get("traceparent") or "").split("-")
    if len(partes) == 4 and len(partes[1]) == 32:
        return partes[1]
    return None


async def send_message(params: dict[str, Any], request: Request) -> dict[str, Any]:
    mensagem = params.get("message") or {}
    texto = " ".join(p.get("text", "") for p in mensagem.get("parts", []) if "text" in p)
    task_id = mensagem.get("taskId")

    if task_id:
        task = TASKS.get(task_id)
        if task is None:
            raise ErroA2A(TASK_NOT_FOUND, f"Task nao encontrada: {task_id}")
        async with task.lock:
            if task.estado in TERMINAIS:
                raise ErroA2A(UNSUPPORTED_OPERATION, f"Task {task_id} esta em estado terminal ({task.estado})")
            if task.estado != "TASK_STATE_INPUT_REQUIRED":
                raise ErroA2A(UNSUPPORTED_OPERATION, f"Task {task_id} nao aguarda entrada ({task.estado})")
            task.trace_id = trace_id_de(request) or task.trace_id
            task.history.append({**mensagem, "contextId": task.context_id})
            await executar(task, retomar(task, texto))
        return {"task": task.json()}

    task = Task(id=f"task-{secrets.token_hex(6)}", context_id=mensagem.get("contextId") or f"ctx-{secrets.token_hex(6)}",
                trace_id=trace_id_de(request) or secrets.token_hex(16))
    TASKS[task.id] = task
    log(f"[a2a] task={task.id} estado={task.estado} trace-id={task.trace_id}")
    task.history.append({**mensagem, "taskId": task.id, "contextId": task.context_id})
    async with task.lock:
        task.mudar("TASK_STATE_WORKING")
        await executar(task, iniciar(task, texto))
    return {"task": task.json()}


async def executar(task: Task, passo) -> None:
    try:
        await passo
    except ErroMCP as e:
        task.pausa = None
        task.mudar("TASK_STATE_FAILED", f"Erro do servidor MCP: {e}")
    except Exception as e:  # falha de transporte etc.
        task.pausa = None
        log(f"[a2a] task={task.id} falhou: {e!r}")
        task.mudar("TASK_STATE_FAILED", "Falha ao falar com o servidor MCP")


async def get_task(params: dict[str, Any], _: Request) -> dict[str, Any]:
    task = TASKS.get(params.get("id", ""))
    if task is None:
        raise ErroA2A(TASK_NOT_FOUND, f"Task nao encontrada: {params.get('id')}")
    return {"task": task.json()}


METODOS = {"SendMessage": send_message, "GetTask": get_task}


async def a2a(request: Request) -> JSONResponse:
    try:
        corpo = await request.json()
    except ValueError:
        return JSONResponse({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}})
    rpc_id = corpo.get("id")
    metodo = METODOS.get(corpo.get("method"))
    if metodo is None:
        erro = {"code": METHOD_NOT_FOUND, "message": f"Metodo nao suportado: {corpo.get('method')}"}
        return JSONResponse({"jsonrpc": "2.0", "id": rpc_id, "error": erro})
    try:
        resultado = await metodo(corpo.get("params") or {}, request)
    except ErroA2A as e:
        return JSONResponse({"jsonrpc": "2.0", "id": rpc_id, "error": {"code": e.codigo, "message": str(e)}})
    return JSONResponse({"jsonrpc": "2.0", "id": rpc_id, "result": resultado})


CARD = {
    "name": "Central de Salas",
    "description": "Reserva salas de reuniao da Hill Valley Tech.",
    "provider": {"organization": "Hill Valley Tech", "url": "https://hillvalley.example"},
    "version": "1.0.0",
    "supportedInterfaces": [{"url": f"{URL_PUBLICA}/a2a", "protocolBinding": "JSONRPC", "protocolVersion": "1.0"}],
    "capabilities": {"streaming": False, "pushNotifications": False, "extendedAgentCard": False},
    "defaultInputModes": ["text/plain"],
    "defaultOutputModes": ["text/plain"],
    "skills": [{
        "id": "reservar-sala",
        "name": "Reservar sala",
        "description": "Reserva uma sala em um intervalo. Se houver conflito, pergunta qual alternativa usar.",
        "tags": ["salas", "agenda"],
        "inputModes": ["text/plain"],
        "outputModes": ["text/plain"],
        "examples": ["reservar sala=sala-garagem inicio=2026-11-03T14:00:00-03:00 "
                     "fim=2026-11-03T15:00:00-03:00 responsavel=Marty"],
    }],
}


async def agent_card(_: Request) -> JSONResponse:
    return JSONResponse(CARD)


@asynccontextmanager
async def ciclo_de_vida(_: Starlette):
    yield
    await mcp.fechar()


app = Starlette(
    routes=[Route("/.well-known/agent-card.json", agent_card), Route("/a2a", a2a, methods=["POST"])],
    lifespan=ciclo_de_vida,
)

if __name__ == "__main__":
    log(f"[a2a] agente em {URL_PUBLICA} (card em /.well-known/agent-card.json, MCP em {URL_MCP})")
    uvicorn.run(app, host="0.0.0.0", port=PORTA, log_level="warning")
