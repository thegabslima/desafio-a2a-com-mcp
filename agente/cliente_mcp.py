"""Cliente MCP minimo (revisao 2026-07-28) usado pelo agente como host.

Cada request e autocontido: `_meta` obrigatorio, capability de elicitation em
form mode, headers espelhados do corpo e id novo. O resultado volta cru, de modo
que o `input_required` chega ao agente em vez de ser respondido aqui.
"""

from __future__ import annotations

import itertools
import secrets
from typing import Any

import httpx

PROTOCOLO = "2026-07-28"
CAPABILITIES = {"elicitation": {"form": {}}}
CLIENT_INFO = {"name": "agente-central-de-salas", "version": "1.0.0"}


class ErroMCP(Exception):
    def __init__(self, erro: dict[str, Any]):
        super().__init__(f"{erro.get('code')}: {erro.get('message')}")
        self.erro = erro


def novo_traceparent(trace_id: str) -> str:
    """Mesmo trace-id, span-id novo a cada request."""
    return f"00-{trace_id}-{secrets.token_hex(8)}-01"


class ClienteMCP:
    def __init__(self, url: str):
        self.url = url
        self.http = httpx.AsyncClient(timeout=30)
        self._ids = itertools.count(1)

    async def fechar(self) -> None:
        await self.http.aclose()

    async def request(self, metodo: str, params: dict[str, Any], trace_id: str) -> dict[str, Any]:
        corpo = {
            "jsonrpc": "2.0",
            "id": next(self._ids),  # id novo em todo request, inclusive no retry do MRTR
            "method": metodo,
            "params": {
                **params,
                "_meta": {
                    "io.modelcontextprotocol/protocolVersion": PROTOCOLO,
                    "io.modelcontextprotocol/clientInfo": CLIENT_INFO,
                    "io.modelcontextprotocol/clientCapabilities": CAPABILITIES,
                    "traceparent": novo_traceparent(trace_id),
                },
            },
        }
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": PROTOCOLO,
            "Mcp-Method": metodo,
        }
        if metodo == "tools/call":
            headers["Mcp-Name"] = params["name"]
        elif metodo == "resources/read":
            headers["Mcp-Name"] = params["uri"]
        resposta = (await self.http.post(self.url, json=corpo, headers=headers)).json()
        if "error" in resposta:
            raise ErroMCP(resposta["error"])
        return resposta["result"]

    async def listar_tools(self, trace_id: str) -> list[dict[str, Any]]:
        return (await self.request("tools/list", {}, trace_id))["tools"]

    async def ler_resource(self, uri: str, trace_id: str) -> str:
        resultado = await self.request("resources/read", {"uri": uri}, trace_id)
        return resultado["contents"][0]["text"]

    async def chamar_tool(self, nome: str, argumentos: dict[str, Any], trace_id: str, *,
                          input_responses: dict[str, Any] | None = None,
                          request_state: str | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {"name": nome, "arguments": argumentos}
        if input_responses is not None:
            params["inputResponses"] = input_responses
        if request_state is not None:
            params["requestState"] = request_state
        return await self.request("tools/call", params, trace_id)
