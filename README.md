# Roblox Multi-Agent Orchestrator — v0.4 (papéis simétricos)

Orquestrador multiagente local para o **AFK Evolution Simulator**. **Gemini 4 Argon High** (Antigravity CLI) e
**DeepSeek V4.1 Flash Free** (OpenCode 2.x) planejam, implementam e revisam. Eles conversam por **A2A 1.0**
(SDK oficial `a2a-sdk`), com uma regra central:

> **Quem implementa nunca revisa o próprio trabalho.** O revisor é sempre o outro agente.

"Conversa" aqui significa troca de mensagens estruturadas (contratos Pydantic) mediada pelo orquestrador, não chat livre.

## Fluxo
```
1. Planejamento     Gemini planeja (se estiver sem cota, o DeepSeek planeja -> plano "planned_by_fallback")
2. Atribuição       turno 1: o plano PROPÕE assigned_to + motivo para cada subtarefa
                    turno 2: o outro agente aceita/contesta TUDO em uma única chamada
                    desempate: config/assignment_rules.json (sem 3ª rodada); Gemini sem cota -> DeepSeek assume
Por subtarefa (dependentes só começam quando a dependência está APROVADA):
3. Implementação    assigned_to implementa (arquivos reservados, tudo ou nada)
4. Checagens        runner sem shell (selene/stylua/luau-analyze, desligadas por padrão)
                    falhou -> volta ao implementador SEM gastar revisão de LLM (MAX_CHECK_FIX_ATTEMPTS)
                    falha idêntica repetida -> escala na hora (sem progresso)
5. Revisão          o OUTRO agente revisa em modo somente leitura (agy --mode plan / agente Plan do OpenCode)
                    checklist: critérios de aceitação, APIs do Roblox, casos de borda, segurança cliente/servidor
                    achado sem evidência (arquivo + linha + trecho literal a ±5 linhas) é descartado
                    todos descartados -> UMA reformulação -> persistiu -> review_unverified -> needs_human
                    revisor alterou arquivos -> veredito invalidado -> needs_human
6. Ajuste           até MAX_REVIEW_CYCLES ciclos; discordância persistente -> needs_human (resumo dos dois lados)
```
Hierarquia em discordância: **checagem automática > revisor com evidência > escalonar para você**.

## Iniciar (Windows PowerShell)
Use o atalho **`Iniciar Orquestrador.bat`** (copie para a Área de Trabalho) ou dois terminais:
```powershell
cd C:\Users\Felipe\Downloads\roblox_orchestrator_starter
.\run_opencode.ps1      # terminal 1 (deixe aberto)
.\start.ps1             # terminal 2
```
Painel: http://127.0.0.1:8787/dashboard/ · API: http://127.0.0.1:8787/docs

## Configuração (.env)
O `.env` existente continua válido: todas as chaves novas têm valor padrão. As que importam nesta versão:

| Chave | Padrão | Para quê |
|---|---|---|
| `OPENCODE_AGENT` | (vazio) | Agente do OpenCode para **implementar** (use `Build`) |
| `OPENCODE_REVIEW_AGENT` | `Plan` | Agente **somente leitura** do OpenCode para planejar/revisar |
| `AGY_REVIEW_MODE` | `plan` | `--mode` do agy nas skills somente leitura |
| `AGY_IMPLEMENT_MODE` | (vazio) | `--mode` ao implementar; vazio = comportamento validado do agy |
| `MAX_REVIEW_CYCLES` | 2 | Ciclos de ajuste pedidos pelo revisor |
| `MAX_CHECK_FIX_ATTEMPTS` | 2 | Correções por falha de checagem (contador separado) |
| `MAX_IMPLEMENTATION_ATTEMPTS` | 4 | Teto global de implementações por subtarefa |
| `MAX_BLOCKED_SUBTASKS` | 3 | Ao atingir, o workflow é pausado (`paused`) |
| `ASSIGNMENT_NEGOTIATION` | true | `false` remove a chamada de aceite/contestação |

`config/assignment_rules.json`: tabela de desempate e `default_agent` (Gemini por padrão; use `deepseek` para poupar a cota do Argon).
`config/checks.json`: checagens automáticas, **todas desligadas**. Confira as flags da sua instalação antes de ligar.

## Verificar sem gastar tokens
```powershell
.\scripts\check_integrations.ps1                 # agy (--mode/--add-dir), OpenCode (Build/Plan), modelo, rotas, quota, checagens, MCP
.\scripts\check_integrations.ps1 -Orchestrator   # + orquestrador e Agent Cards A2A
```

## Operação
| Ação | Como |
|---|---|
| Nova tarefa | Painel, ou `POST /tasks` com `{"goal": "...", "mode": "plan_only" \| "full"}` |
| Decidir subtarefa em `needs_human` | Botões no painel, ou `POST /tasks/{id}/subtasks/{sid}/decision` com `{"action": "approve\|retry\|reassign\|reject", "note": "..."}` |
| Retomar (depois das decisões, cota liberada, pausa) | Botão **Retomar**, ou `POST /tasks/{id}/resume` |
| Liberar cota manualmente | `POST /agents/{gemini\|deepseek}/clear-quota` |
| Estado das checagens | `GET /checks/status` |
| Métricas de negociação/chamadas | Aba "Plano/Métricas/Resultado" do painel, ou campo `metrics` de `GET /tasks/{id}` |

Exemplo em PowerShell (decisão + retomada):
```powershell
$t = 'ID_DA_TAREFA'; $s = 'ID_DA_SUBTAREFA'
$body = @{ action = 'retry'; note = 'Siga o padrão do AGENTS.md para RemoteEvents' } | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:8787/tasks/$t/subtasks/$s/decision" -ContentType 'application/json; charset=utf-8' -Body ([Text.Encoding]::UTF8.GetBytes($body))
Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:8787/tasks/$t/resume"
```

**Gemini sem cota:** o DeepSeek planeja (o plano fica marcado como fallback) e implementa, mas a subtarefa fica em
`awaiting_review`. Não existe autorrevisão. Quando o Gemini voltar, **Retomar** vai direto para a revisão, que também
valida o plano. Ao acumular `MAX_BLOCKED_SUBTASKS` subtarefas nesse estado, o workflow pausa.

## Testes
```powershell
.\scripts\run_tests.ps1        # testes com MOCKS (modelos simulados; o protocolo A2A é o SDK real)
.\scripts\run_tests.ps1 -Live  # + testes reais sem tokens
```
Testes reais dos papéis (**consomem cota**; rodam num workspace de rascunho, não no jogo):
```powershell
.\scripts\manual_role_test.ps1 -Implementer deepseek   # DeepSeek implementa, Gemini revisa
.\scripts\manual_role_test.ps1 -Implementer gemini     # Gemini implementa, DeepSeek revisa
```

## Banco de dados
`orchestrator.db` (schema 4) e `orchestrator_a2a.db` (tarefas A2A do SDK). Antes de migrar, o banco anterior é copiado
para `orchestrator.db.bak-v<versão>` (uma única vez). A migração só adiciona colunas e não apaga nada.

## Segurança
Escuta só em 127.0.0.1; senha e `Authorization` nunca vão para logs, banco ou API; `.env` só é lido; sem shell;
Git apenas com subcomandos de leitura; caminhos fora do workspace são recusados; reserva de arquivos "tudo ou nada";
paralelismo só entre subtarefas sem arquivos em comum.

## Estrutura
```
agents/          base.py (contrato + QuotaCooldown), gemini_adapter.py, opencode_adapter.py, opencode_contract.py, mock_adapters.py
communication/   a2a_server.py (Agent Cards + executor por skill), a2a_client.py, messaging.py
orchestrator/    coordinator.py, assignment.py, checks.py, evidence.py, prompts.py, schemas.py, task_manager.py,
                 agent_registry.py, file_locks.py, git_integration.py
memory/ database/ mcp_bridge/ api/ dashboard/ config/ scripts/ tools/ tests/ docs/
```
