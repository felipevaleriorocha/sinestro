# Roblox Multi-Agent Orchestrator — starter v0.2 (OpenCode 2.x)

Protótipo local para coordenar Gemini/Antigravity CLI e DeepSeek/OpenCode no mesmo workspace Roblox.

## O que esta versão faz

1. Recebe uma solicitação em `POST /tasks`.
2. Usa Gemini pelo Antigravity CLI como Tech Lead para criar um plano JSON.
3. Distribui subtarefas entre Gemini e DeepSeek.
4. Aciona `agy` e o servidor HTTP do OpenCode 2.x.
5. Registra plano, resultados e revisão em SQLite.
6. Pede ao Gemini uma revisão final do workspace.

A versão 0.2 usa as rotas `/api/` do OpenCode 2.x, autenticação HTTP Basic e o modelo `Model.Ref` da API atual. O fluxo é serializado para evitar alterações simultâneas nos mesmos arquivos. Ainda não é um servidor A2A formal; é um primeiro orquestrador com comunicação mediada entre os agentes.

## Pré-requisitos

- Windows 10/11.
- Python 3.11+ (`py -3 --version`).
- OpenCode 2.x instalado e autenticado.
- Antigravity CLI (`agy`) instalado e autenticado.
- Roblox Studio com o projeto correto aberto, caso queira usar o MCP do Studio.

## 1. Configurar `.env`

Copie `.env.example` para `.env` e configure:

```env
PROJECT_ROOT=C:/Users/Felipe/.gemini/antigravity/scratch/afk-evolution-simulator
OPENCODE_URL=http://127.0.0.1:4096
OPENCODE_MODEL=opencode/deepseek-v4.1-flash-free
OPENCODE_VARIANT=high
OPENCODE_AGENT=build
OPENCODE_SERVER_USERNAME=opencode
OPENCODE_SERVER_PASSWORD=SENHA_GERADA_PARA_O_SERVIDOR
AGY_BIN=agy
AGY_MODEL=gemini-4-argon-high
AGY_EFFORT=high
```

Não compartilhe o conteúdo do `.env`; ele contém a senha do servidor local.

## 2. Iniciar o servidor OpenCode

No PowerShell, dentro desta pasta:

```powershell
.\run_opencode.ps1
```

O script carrega usuário/senha do `.env`, muda o diretório de trabalho para `PROJECT_ROOT` e chama `opencode.cmd` (evita o bloqueio de `opencode.ps1` pela Execution Policy). Deixe o terminal aberto.

## 3. Testar a API OpenCode 2.x

Em outro PowerShell, teste `/api/info`, `/api/model` e `/api/agent`. O modelo `deepseek-v4.1-flash-free` precisa aparecer como `enabled: true`.

## 4. Iniciar o orquestrador

Em outro terminal, execute:

```powershell
.\start.ps1
```

Depois abra `http://127.0.0.1:8787/docs`.

## 5. Enviar uma tarefa

Use `POST /tasks`:

```json
{
  "goal": "Analise o projeto e proponha uma pequena melhoria sem editar arquivos. Respeite AGENTS.md e os padrões existentes."
}
```

Consulte `GET /tasks/{id}` para acompanhar plano, execução, revisão e eventuais erros.

## Segurança e limites

- Orquestrador e OpenCode escutam somente em `127.0.0.1`.
- O OpenCode usa HTTP Basic Auth se as credenciais estiverem definidas no `.env`.
- Faça commit/backup antes de permitir alterações no projeto.
- O fluxo serializa as tarefas; não é seguro permitir que dois agentes alterem os mesmos arquivos em paralelo.
- Não use `--dangerously-skip-permissions` como atalho. Configure permissões específicas para ações de agentes.
- O A2A formal (Agent Cards, endpoints A2A e SDK `a2a-sdk`) ainda não está implementado. Nesta versão, as mensagens passam pelo orquestrador.
