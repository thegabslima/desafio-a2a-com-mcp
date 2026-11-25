# A Ponte: agente A2A com MCP por dentro

Dois processos Python:

- `servidor-mcp/servidor.py` — servidor MCP (SDK oficial `mcp==2.3.0`, revisão `2026-07-28`) em Streamable HTTP na porta `7301`, endpoint `/mcp`.
- `agente/agente.py` — agente A2A v1.0 (JSON-RPC) na porta `7300`, endpoint `/a2a` e card em `/.well-known/agent-card.json`. Por dentro é host MCP e fala com o servidor por HTTP. Não usa LLM.

## Como rodar

Requisitos: [uv](https://docs.astral.sh/uv/) (ele instala o Python 3.12 e as dependências travadas em `uv.lock`).

```bash
git clone https://github.com/thegabslima/desafio-a2a-com-mcp.git
cd desafio-a2a-com-mcp
uv sync
```

Terminal 1, servidor MCP (o segredo do `requestState` vem do ambiente, com pelo menos 32 bytes):

```bash
export REQUEST_STATE_SECRET=$(uv run python -c "import secrets; print(secrets.token_hex(32))")
uv run python servidor-mcp/servidor.py
```

No PowerShell: `$env:REQUEST_STATE_SECRET = uv run python -c "import secrets; print(secrets.token_hex(32))"`.
Para testar o retry depois de reiniciar o servidor, reinicie com o mesmo valor.

Terminal 2, agente:

```bash
uv run python agente/agente.py
```

Terminal 3, validador (com os dois processos recém-iniciados):

```bash
uv run python validador/validar.py --agente http://localhost:7300 --mcp http://localhost:7301
```

Variáveis opcionais: `MCP_PORT` (7301), `AGENTE_PORT` (7300), `AGENTE_URL` (URL pública no card), `MCP_URL` (`http://localhost:7301/mcp`).

## Onde a ponte acontece

Em [`agente/agente.py`](agente/agente.py), `aplicar_resultado` traduz o resultado do `tools/call`: quando ele chega com `resultType: input_required`, o agente guarda chave, `requestState` e opções em uma `Pausa` presa à Task e muda a Task para `TASK_STATE_INPUT_REQUIRED` com a linha `alternativas: ...`. Na continuação, `retomar` converte `escolha=<id>` em `accept` (ou `escolha=recusar` em `decline`) e repete o `tools/call` original com id novo, levando `inputResponses` com a mesma chave e o `requestState` ecoado sem modificação. Do lado do servidor, o resolver `escolha_de_sala` em [`servidor-mcp/servidor.py`](servidor-mcp/servidor.py) devolve um `Elicit` só quando há conflito, e o SDK monta o `input_required`.

O cliente MCP do agente ([`agente/cliente_mcp.py`](agente/cliente_mcp.py)) foi escrito com `httpx` de propósito: o `Client` do SDK declara `elicitation: {form, url}` e responde elicitations por callback, e aqui o agente precisa declarar só `{"elicitation": {"form": {}}}` e enxergar o `input_required` cru.

## Decisões técnicas

- **`requestState`**: protegido pelo `RequestStateSecurity` do SDK com a chave de `REQUEST_STATE_SECRET` (AES-256-GCM, chave derivada por HKDF). O envelope é cifrado e autenticado, e vinculado ao método, à tool e ao digest dos argumentos originais. Adulteração, expiração ou argumentos trocados no retry dão `-32602`. Como a chave vem do ambiente, o estado continua válido depois de reiniciar o servidor.
- **Validade**: 10 minutos (`TTL_REQUEST_STATE`).
- **Estado das Tasks**: em memória no agente (`TASKS`), cada Task com seu próprio `asyncio.Lock`. O `requestState` fica só na `Pausa` da Task e nunca é serializado nas respostas A2A.
- **Reservas**: em memória no servidor MCP, carregadas de `dados/` na subida.
- **Log**: um middleware ASGI escreve no stderr do servidor MCP o método, o id, a tool/URI e o `traceparent` de cada request. O agente propaga o trace-id do header `traceparent` recebido via A2A, com span-id novo em cada request MCP.

## Saída do validador

```
trace-id desta execucao: f7e327e2157f86ce1d1b8b0c1fd9c67d
procure esse valor no stderr do servidor MCP para conferir a propagacao do traceparent.

PASS 01 tools/list traz as tres tools
PASS 02 toda tool tem inputSchema de objeto
PASS 03 listar_salas devolve structuredContent e o mesmo JSON em texto
PASS 04 _meta sem protocolVersion devolve -32602 e HTTP 400
PASS 05 _meta sem clientCapabilities devolve -32602 e HTTP 400
PASS 06 tool inexistente e recusada, por -32602 ou por isError
PASS 07 resources/read de politica://uso devolve a politica
PASS 08 resources/read de URI inexistente devolve -32602
PASS 09 sala inexistente devolve isError com a mensagem exata
PASS 10 fora da janela devolve isError com a mensagem exata
PASS 11 duracao acima de 2h devolve isError com a mensagem exata
PASS 12 intervalo invertido devolve isError com a mensagem exata
PASS 13 conflito devolve input_required com inputRequests e requestState
PASS 14 a elicitation e form mode e oferece as alternativas na ordem certa
PASS 15 conflito sem a capability elicitation devolve -32021 e HTTP 400
PASS 16 retry com inputResponses e requestState conclui a reserva
PASS 17 requestState adulterado e rejeitado com -32602
PASS 18 argumentos adulterados no retry nao tomam efeito
PASS 19 recusa conclui sem reservar e sem isError
PASS 20 conflito sem alternativa possivel devolve isError com a mensagem exata

PASS 21 agent card responde 200 no well-known com JSON
PASS 22 o card declara a interface JSON-RPC com url e versao 1.0
PASS 23 o card declara a skill reservar-sala
PASS 24 SendMessage com sala livre conclui a Task
PASS 25 o artifact chama reserva e traz a versao da politica
PASS 26 GetTask devolve id, contextId e estado corrente
PASS 27 SendMessage com sala ocupada pausa a Task
PASS 28 a Task pausada lista as alternativas na ordem certa
PASS 29 escolha fora do enum mantem a Task pausada
PASS 30 a continuacao conclui a Task na sala escolhida
PASS 31 SendMessage em Task terminal e recusado
PASS 32 a recusa termina a Task em CANCELED
PASS 33 duas Tasks pausadas ao mesmo tempo concluem cada uma com a sua reserva
PASS 34 nenhuma resposta A2A carrega o requestState
PASS 35 sala inexistente termina a Task em FAILED com a mensagem da tool
PASS 36 o agente e deterministico: o mesmo pedido produz a mesma pausa

resumo: 36 passaram, 0 falharam, de 36 verificacoes
```
