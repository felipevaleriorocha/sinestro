# Multi-Agent Orchestrator

Orquestrador multiagente **local e autônomo** para coordenação, engenharia e entrega colaborativa de software com múltiplos agentes de IA que planejam, implementam e **revisam o trabalho uns dos outros**. Os agentes operam e comunicam-se via protocolo **A2A 1.0** (SDK oficial [`a2a-sdk`](https://github.com/a2aproject/a2a-python)), utilizando contratos de dados estruturados e validação determinística, sem dependência de chat livre.

> **Regra central do sistema:** quem implementa nunca revisa o próprio trabalho. A revisão cruzada é obrigatoriamente atribuída a outro agente independente.

| Agente | Canal de Execução | Configuração Base |
|---|---|---|
| **Gemini** | Antigravity CLI (`agy`), via subprocesso controlado | `AGY_MODEL` |
| **DeepSeek** | API HTTP local / OpenCode Engine | `OPENCODE_MODEL` |

Os modelos e provedores são configurados de forma declarativa via ambiente (`.env`); o núcleo de orquestração não mantém acoplamento fixo a nenhum modelo específico.

**Estado do projeto:** Versão **0.5.0** (Banco SQLite **Schema 6**). Inclui validação estática de dependências via **Dependency Analyzer**, execução determinística e sequencial por padrão (`PARALLEL_EXECUTION=false`), rastreabilidade de estágios (`stage_log`) e suporte nativo a isolamento de recursos e ferramentas externas via **Model Context Protocol (MCP)**.

---

## Fluxo de Execução de Tarefas

```
1. Planejamento           Agente planejador decompõe a meta em subtarefas estruturadas com critérios verificáveis
2. Validação Estrutural   Dependency Analyzer valida o DAG (sem LLM); se inválido, concede 1 reformulação
3. Atribuição             Turno 1: proposta no plano; Turno 2: aceite ou contestação técnica do outro agente
                          Desempate determinístico via config/assignment_rules.json
4. Implementação          Agente atribuído atua estritamente sobre arquivos e recursos reservados
5. Checagens Automáticas  Linters, analisadores estáticos e testes locais executam antes de revisões por LLM
6. Revisão Cruzada        O OUTRO agente revisa em modo somente leitura (evidência estrita: arquivo, linha e trecho)
7. Integração de Onda     Validação de checagens automáticas na união dos arquivos do grupo topológico
8. Revisão Final          Integração global consolidada (interfaces, contratos, consistência e estado compartilhado)
```

Hierarquia em caso de divergência: **checagem automática > revisor com evidência verificável > escalonamento humano (`needs_human`)**.

---

### Estados de Conclusão e Revisão Final

O campo `final_review_state` é estritamente persistido, e a função pura `final_status()` (`orchestrator/task_manager.py`) atua como a autoridade única de transição do workflow:

| `final_review_state` | Status do Workflow (com subtarefas aprovadas) |
|---|---|
| `passed` | `completed` |
| `disabled` (`FINAL_REVIEW=false`) | `completed` (com sinalização de revisão desativada) |
| `accepted_by_human` | `completed` |
| `skipped` / `failed` | `completed_unreviewed` (**Retomar** executa estritamente a revisão final pendente) |
| `changes_requested` / `rejected` / `unverified` / `invalidated` | `needs_human` |
| `rejected_by_human` | `failed` |

Caso todas as subtarefas tenham sido implementadas por um único agente, o outro agente assume integralmente a revisão final. Em execuções com autoria mista, o revisor tem sua aprovação aplicada apenas às tarefas de terceiros, registrando explicitamente as tarefas de autoria própria (`self_authored_subtasks`) para cobertura auditada.

---

## Pré-requisitos

- Python 3.11 ou superior (`py -3 --version` ou `python3 --version`)
- Antigravity CLI (`agy`) configurado e autenticado
- OpenCode Engine ou servidor de inferência local compatível
- Git no `PATH` (utilizado estritamente em **modo somente leitura** para snapshots e diffs)
- Opcional: ferramentas de análise estática e linters locais (configurados em `config/checks.json`)
- Opcional: Node.js (apenas para suíte de testes de renderização estática do dashboard)

---

## Instalação e Inicialização

```bash
git clone <url-do-repositorio> multi_agent_orchestrator
cd multi_agent_orchestrator
cp .env.example .env    # Configure PROJECT_ROOT e credenciais necessárias
```

Em ambiente Windows (PowerShell):

```powershell
# Terminal 1: Servidor de backend do motor de suporte (se aplicável)
.\run_opencode.ps1

# Terminal 2: Orquestrador (cria o .venv e instala dependências automaticamente na 1ª execução)
.\start.ps1
```

* **Painel de Operações:** `http://127.0.0.1:8787/dashboard/`
* **Especificação da API (OpenAPI/Swagger):** `http://127.0.0.1:8787/docs`

---

## Configuração do Ambiente (`.env`)

O arquivo `.env` nunca deve ser versionado.

| Variável | Padrão | Descrição |
|---|---|---|
| `PROJECT_ROOT` | — | Caminho absoluto do workspace gerenciado |
| `OPENCODE_URL` | `http://127.0.0.1:4096` | Endpoint do servidor local de desenvolvimento |
| `OPENCODE_MODEL` | `opencode/deepseek-v4.1-flash-free` | Identificador do modelo (`provider/model`) |
| `OPENCODE_AGENT` | `Build` | Persona operacional de implementação |
| `OPENCODE_REVIEW_AGENT` | `Plan` | Persona somente leitura para planejamento e análise |
| `AGY_BIN` | `agy` | Executável ou wrapper de linha de comando |
| `AGY_MODEL` | `gemini-4-argon-high` | Modelo principal de raciocínio/arquitetura |
| `AGY_REVIEW_MODE` | `plan` | Flag `--mode` aplicada em tarefas estritamente analíticas |
| `PARALLEL_EXECUTION` | `false` | Concorrência entre subtarefas independentes (padrão seguro) |
| `MAX_PARALLEL_SUBTASKS` | `1` | Limite de paralelismo simultâneo |
| `MAX_REVIEW_CYCLES` | `2` | Limite de ciclos de correção solicitados pelo revisor |
| `MAX_CHECK_FIX_ATTEMPTS` | `2` | Tentativas automáticas de correção por falhas de linters |
| `MAX_IMPLEMENTATION_ATTEMPTS` | `4` | Teto global de tentativas por subtarefa antes de escalonar |
| `MAX_BLOCKED_SUBTASKS` | `3` | Pausa defensiva de workflow ao acumular tarefas bloqueadas |
| `FINAL_REVIEW` | `true` | Ativa a etapa global de integração e validação final |
| `ORCH_MOCK_AGENTS` | `false` | `true` ativa agentes simulados em memória (testes de fluxo) |

---

## Dependency Analyzer (DAG Determinístico)

Antes de executar ou delegar qualquer código, o plano de tarefas é submetido ao `DependencyAnalyzer` (`orchestrator/dependency_analyzer.py`), que atua sem invocar modelos de linguagem e sem executar processos:

1. **Validação Estrutural:** Identifica e bloqueia IDs vazios, IDs duplicados, referências inexistentes em `depends_on`, loops recursivos e caminhos externos ao workspace.
2. **Classificação Estática de Relações:**
   * `PARALLEL`: Subtarefas com grafos totalmente disjuntos, sem dependência mútua e sem compartilhamento de arquivos ou recursos.
   * `SEQUENTIAL`: Dependência explícita, incerteza técnica declarada ou carregamento dinâmico.
   * `SHARED`: Compartilhamento de arquivos, contratos comuns, esquemas de persistência ou recursos MCP.
3. **Agrupamento Topológico em Ondas:** Ordena a execução em grupos cronológicos. Mesmo em modo sequencial, os grupos governam a ordem de bloqueio e a checagem de integração.

---

## Operação via API REST

| Ação | Método / Endpoint | Payload / Descrição |
|---|---|---|
| Iniciar workflow | `POST /tasks` | `{"goal": "...", "mode": "plan_only" \| "full"}` |
| Consultar workflow | `GET /tasks/{id}` | Retorna status, DAG, eventos, métricas e etapas |
| Reanalisar plano | `POST /tasks/{id}/analyze` | Recalcula relações e grupos topológicos sem LLM |
| Relatório operacional | `GET /tasks/{id}/report` | Consolidação de estágios, arquivos modificados e métricas |
| Retomar workflow | `POST /tasks/{id}/resume` | Retoma workflows bloqueados, pausados ou pendentes de revisão |
| Decisão de subtarefa | `POST /tasks/{id}/subtasks/{sid}/decision` | Ação humana: `approve`, `retry`, `reassign` ou `reject` |
| Decisão de revisão final | `POST /tasks/{id}/final-review/decision` | Ação humana: `accept` ou `reject` |
| Cancelar workflow | `POST /tasks/{id}/cancel` | Sinaliza cancelamento seguro e auditoria de parada |
| Liberação manual de cota | `POST /agents/{name}/clear-quota` | Reseta cooldown persistido após restabelecimento de cota |
| Status de integrações | `GET /checks/status` | Exibe ferramentas de validação instaladas e ativas |
| Gerenciador de locks | `GET /locks`, `GET /locks/history` | Recursos atualmente reservados e histórico de auditoria |

---

## Bateria de Testes e Validação

```powershell
# Execução da suíte completa de testes unitários e de integração com Mocks
.\scripts\run_tests.ps1

# Testes de integração de infraestrutura local (sem consumo de tokens)
.\scripts\run_tests.ps1 -Live

# Teste operacional ponta a ponta com modelos reais (consome cota em workspace isolado)
.\scripts\manual_role_test.ps1 -Implementer deepseek
.\scripts\manual_role_test.ps1 -Implementer gemini
```

---

## Segurança e Concorrência

* **Isolamento de rede:** O serviço expõe endpoints exclusivamente em `127.0.0.1`.
* **Sanitização de dados:** Chaves, senhas e cabeçalhos de autenticação são ofuscados antes de qualquer registro em log ou banco de dados.
* **Fronteira de workspace:** Caminhos absolutos ou relativos fora de `PROJECT_ROOT` são terminantemente rejeitados por todas as camadas de validação.
* **Subprocessos controlados:** Ausência de interpretadores de shell arbitrários. Executáveis utilizam listas fechadas de argumentos validados.
* **Reservas Atômicas (`FileLockManager`):** Sistema transacional "tudo-ou-nada" para reservas de arquivos e recursos `mcp:`, com retenção preventiva de locks caso uma parada externa não seja confirmada.

---

## Roadmap

### Fase 0 — Estabilização do Núcleo v0.5.x
- [x] Validação estrutural determinística de dependências e ondas topológicas.
- [x] Persistência do histórico de estágios (`stage_log`) e suporte inicial a retenção de locks.
- [ ] Encerramento auditado de processos externos com reconciliação de locks retidos.
- [ ] Ativação experimental de paralelismo multitarefa controlado (`PARALLEL_EXECUTION=true`).

### Fase 1 — Arquitetura Agnóstica e Perfis de Domínio
- [ ] Formalização da interface `DomainProfile`, desacoplando completamente regras de domínio, prompts e convenções de arquivos do motor central.
- [ ] Extração modular de perfis especializados (desenvolvimento de software, pipelines de dados, automação e análise técnica).
- [ ] Servidor A2A agnóstico orientado a schemas e capacidades declaradas.

### Fase 2 — Workspace Multi-Provider e Ferramental Unificado
- [ ] Abstração completa de provedores (OpenAI, Anthropic, Google, DeepSeek e inferência local via Ollama/vLLM) sem impacto no fluxo de orquestração.
- [ ] Roteamento inteligente de tarefas por matriz de capacidade, custo e latência.
- [ ] Gateway unificado de permissões, execução e reservas de ferramentas via Model Context Protocol (MCP).
- [ ] Recuperação contextual semântica (RAG local) para grandes volumes de documentação e código.

---

## Estrutura do Repositório

```
agents/          Adaptadores de modelos, contratos e controle de cotas (Gemini, OpenCode, Mocks)
api/             Rotas REST FastAPI, injeção de dependências e ciclo de vida da aplicação
communication/   Servidor e cliente A2A (a2a-sdk), Agent Cards e mensageria estruturada
config/          Políticas de dependência, regras de atribuição e checagens automáticas
core/            Configurações (.env), logging sanitizado e gerenciamento seguro de processos
database/        Modelos SQLite (schema 6), migrações aditivas e repositório central
dashboard/       Interface operacional web estática (HTML/CSS/JS sem dependências externas)
mcp_bridge/      Registro e verificação de saúde de servidores MCP
memory/          Gerenciamento de contexto e repositório auditável de decisões técnicas
orchestrator/    Dependency Analyzer, Coordenador de fluxo, gerenciador de locks e schemas
scripts/         Automação de testes, validações e scripts de inicialização
tests/           Suíte de testes unitários, testes de integração e mocks
tools/           Utilitários de diagnóstico, verificação e testes ponta a ponta
```

## Licença

Este projeto está sob a licença MIT. Consulte o arquivo [LICENSE](LICENSE) para obter mais detalhes.