"""Servidor MCP da Central de Salas (Streamable HTTP, revisao 2026-07-28)."""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Annotated, Any, Literal

import uvicorn
from pydantic import BaseModel, Field, create_model

from mcp.server.elicitation import AcceptedElicitation, ElicitationResult
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ResourceError, ToolError
from mcp.server.mcpserver.resolve import Elicit, Resolve
from mcp.server.request_state import RequestStateSecurity

DADOS = Path(__file__).resolve().parent.parent / "dados"
PORTA = int(os.environ.get("MCP_PORT", "7301"))
TTL_REQUEST_STATE = 600  # 10 minutos

SALAS: list[dict[str, Any]] = json.loads((DADOS / "salas.json").read_text(encoding="utf-8"))
RESERVAS: list[dict[str, Any]] = json.loads((DADOS / "reservas.json").read_text(encoding="utf-8"))
POLITICA_TEXTO = (DADOS / "politica-de-uso.md").read_text(encoding="utf-8")
POLITICA_VERSAO = POLITICA_TEXTO.splitlines()[0].split(":", 1)[1].strip()

SAO_PAULO = timezone(timedelta(hours=-3))
SAO_PAULO_ABRE, SAO_PAULO_FECHA = time(8, 0), time(20, 0)


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


# ---------------------------------------------------------------- dominio


def sala_por_id(sala_id: str) -> dict[str, Any] | None:
    return next((s for s in SALAS if s["id"] == sala_id), None)


def validar(sala: str, inicio: str, fim: str) -> tuple[datetime, datetime]:
    """Aplica as regras de sala e de politica. Levanta ToolError com a mensagem exata."""
    if sala_por_id(sala) is None:
        raise ToolError(f"Sala inexistente: {sala}")
    try:
        ini, fi = datetime.fromisoformat(inicio), datetime.fromisoformat(fim)
    except ValueError:
        raise ToolError("Intervalo invalido: fim deve ser posterior a inicio") from None
    if fi <= ini:
        raise ToolError("Intervalo invalido: fim deve ser posterior a inicio")
    ini_sp = ini.astimezone(SAO_PAULO).replace(tzinfo=None)
    fim_sp = fi.astimezone(SAO_PAULO).replace(tzinfo=None)
    abre = datetime.combine(ini_sp.date(), SAO_PAULO_ABRE)
    fecha = datetime.combine(ini_sp.date(), SAO_PAULO_FECHA)
    if ini_sp < abre or fim_sp > fecha:
        raise ToolError("Fora da janela de uso: a politica permite reservas entre 08:00 e 20:00")
    if fi - ini > timedelta(hours=2):
        raise ToolError("Duracao acima do limite: a politica permite no maximo 2 horas")
    return ini, fi


def conflitos(sala: str, ini: datetime, fi: datetime) -> list[dict[str, Any]]:
    return [
        r
        for r in RESERVAS
        if r["sala"] == sala
        and datetime.fromisoformat(r["inicio"]) < fi
        and ini < datetime.fromisoformat(r["fim"])
    ]


def alternativas(sala: str, ini: datetime, fi: datetime) -> list[str]:
    capacidade = sala_por_id(sala)["capacidade"]
    livres = [
        s for s in SALAS if s["id"] != sala and s["capacidade"] >= capacidade and not conflitos(s["id"], ini, fi)
    ]
    livres.sort(key=lambda s: (s["capacidade"], s["id"]))
    return [s["id"] for s in livres[:3]]


def criar_reserva(sala: str, inicio: str, fim: str, responsavel: str) -> ReservaOut:
    numero = max(int(r["id"].split("-")[1]) for r in RESERVAS) + 1 if RESERVAS else 1
    reserva = {"id": f"res-{numero:04d}", "sala": sala, "inicio": inicio, "fim": fim, "responsavel": responsavel}
    RESERVAS.append(reserva)
    return ReservaOut(
        reserva=reserva["id"], reservado=True, sala=sala, inicio=inicio, fim=fim,
        responsavel=responsavel, politica=POLITICA_VERSAO,
    )


# ---------------------------------------------------------------- schemas


class SalaOut(BaseModel):
    id: str
    nome: str
    capacidade: int
    recursos: list[str]


class ListaDeSalas(BaseModel):
    salas: list[SalaOut]


class ConflitoOut(BaseModel):
    id: str
    inicio: str
    fim: str
    responsavel: str


class Disponibilidade(BaseModel):
    sala: str
    livre: bool
    conflitos: list[ConflitoOut]


class ReservaOut(BaseModel):
    reserva: str | None = None
    reservado: bool = True
    sala: str | None = None
    inicio: str | None = None
    fim: str | None = None
    responsavel: str | None = None
    politica: str | None = None
    motivo: str | None = None


def schema_escolha(opcoes: list[str]) -> type[BaseModel]:
    """Schema plano da elicitation: uma propriedade `sala` restrita as alternativas."""
    tipo = Literal[tuple(opcoes)]  # type: ignore[valid-type]
    return create_model("EscolhaDeSala", sala=(tipo, Field(title="Sala", description="Sala alternativa escolhida")))


# ---------------------------------------------------------------- servidor

segredo = os.environ.get("REQUEST_STATE_SECRET")
if not segredo or len(segredo.encode()) < 32:
    raise SystemExit("defina REQUEST_STATE_SECRET com pelo menos 32 bytes (veja o README)")

mcp = MCPServer(
    name="central-de-salas",
    version="1.0.0",
    # AES-256-GCM do SDK: o requestState e cifrado e autenticado, com expiracao
    # e vinculado ao metodo, a tool e ao digest dos argumentos originais.
    request_state_security=RequestStateSecurity(keys=[segredo], ttl=TTL_REQUEST_STATE),
)


@mcp.tool(description="Lista todas as salas com capacidade e recursos.")
def listar_salas() -> ListaDeSalas:
    return ListaDeSalas(salas=[SalaOut(**s) for s in SALAS])


@mcp.tool(description="Diz se uma sala esta livre no intervalo, e quais reservas conflitam.")
def consultar_disponibilidade(sala: str, inicio: str, fim: str) -> Disponibilidade:
    ini, fi = validar(sala, inicio, fim)
    em_conflito = [ConflitoOut(**{k: r[k] for k in ("id", "inicio", "fim", "responsavel")}) for r in conflitos(sala, ini, fi)]
    return Disponibilidade(sala=sala, livre=not em_conflito, conflitos=em_conflito)


def escolha_de_sala(sala: str, inicio: str, fim: str) -> Elicit[Any] | None:
    """Resolver MRTR: so pergunta quando ha conflito. Roda antes do corpo da tool."""
    ini, fi = validar(sala, inicio, fim)
    if not conflitos(sala, ini, fi):
        return None
    opcoes = alternativas(sala, ini, fi)
    if not opcoes:
        raise ToolError("Sem alternativas disponiveis no intervalo")
    return Elicit("A sala pedida esta ocupada nesse intervalo. Escolha uma alternativa.", schema_escolha(opcoes))


@mcp.tool(description="Reserva uma sala. Se o intervalo estiver ocupado, pergunta qual alternativa usar.")
def reservar_sala(
    sala: str,
    inicio: str,
    fim: str,
    responsavel: str,
    escolha: Annotated[ElicitationResult[Any], Resolve(escolha_de_sala)],
) -> ReservaOut:
    if not isinstance(escolha, AcceptedElicitation):
        return ReservaOut(reservado=False, motivo="recusado")
    destino = sala if escolha.data is None else escolha.data.sala
    return criar_reserva(destino, inicio, fim, responsavel)


@mcp.resource("politica://uso", name="politica-de-uso", mime_type="text/markdown",
              description="Politica de uso das salas. A primeira linha declara a versao.")
def politica() -> str:
    return POLITICA_TEXTO


# ---------------------------------------------------------------- log no stderr


class LogDeRequests:
    """ASGI: registra metodo, id e traceparent de todo request antes de qualquer validacao."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] != "POST":
            return await self.app(scope, receive, send)
        corpo = b""
        mensagens = []
        while True:
            msg = await receive()
            mensagens.append(msg)
            corpo += msg.get("body", b"")
            if not msg.get("more_body"):
                break
        try:
            req = json.loads(corpo)
            meta = (req.get("params") or {}).get("_meta") or {}
            log(f"[mcp] method={req.get('method')} id={req.get('id')} "
                f"name={(req.get('params') or {}).get('name') or (req.get('params') or {}).get('uri') or '-'} "
                f"traceparent={meta.get('traceparent', '-')}")
        except (ValueError, AttributeError):
            log("[mcp] corpo invalido")

        async def replay():
            return mensagens.pop(0) if mensagens else await receive()

        await self.app(scope, replay, send)


def main() -> None:
    app = mcp.streamable_http_app(json_response=True, stateless_http=True, host="0.0.0.0")
    log(f"[mcp] central-de-salas em http://localhost:{PORTA}/mcp (politica {POLITICA_VERSAO})")
    uvicorn.run(LogDeRequests(app), host="0.0.0.0", port=PORTA, log_level="warning")


if __name__ == "__main__":
    main()
