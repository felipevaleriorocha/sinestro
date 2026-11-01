# Plano de Evolução Arquitetural: Workspace Multi-Provider

## Visão do Produto
O objetivo de longo prazo é transformar o orquestrador (atualmente focado em Roblox) num **Workspace Centralizado de IA Multiagente e Multi-Provider**. 
O sistema será capaz de receber uma solicitação, identificar o domínio (Roblox, Web Dev, Data Science, etc.), selecionar os modelos/provedores mais adequados (OpenAI, Anthropic, Google, DeepSeek) e gerir a execução, validação e entrega num único ambiente, eliminando a fragmentação do trabalho com IA.

## Princípios Arquiteturais Inegociáveis
1. **Separação de DAGs:** O DAG de tarefas (dependências entre subtarefas) não se mistura com o DAG de workflow (sequência de etapas de execução).
2. **Core Independente:** O núcleo do sistema é cego ao domínio. A sua responsabilidade é garantir segurança, gestão de locks (retenção estrita), orçamentos, concorrência, idempotência e recuperação de falhas.
3. **Abstração Restrita de Provedores:** `Provider`, `Model`, `Agent` e `Tool` são entidades separadas. O core não presume que todas as APIs possuem as mesmas capacidades (preparação para um futuro *Capability Registry*).
4. **Domínio Passivo:** As regras específicas (Luau, React, etc.) vivem exclusivamente num `DomainProfile`, que injeta instruções, validadores e configurações no Core.

---

## Fase 0: Estabilização (✅ CONCLUÍDA)
- Centralização da liberação de locks (retenção obrigatória caso não haja confirmação de encerramento do processo externo: `expired_retained`).
- Ciclo de vida estrito do `stage_log`, registando o status exato da base de dados e impedindo mascaramento de falhas.
- Injeção exclusiva da política de dependências (`config/dependency_policy.json`).
- Resolução de problemas de encoding e consolidação de testes rigorosos do ciclo topológico e de falhas estruturais.

---

## Fase 1: Extração Passiva do Domínio (✅ CONCLUÍDA)
- Criação da interface abstrata `core/domain.py` (`DomainProfile`).
- Isolamento das regras e prompts de Roblox em `domains/roblox/`.
- Desacoplamento total: zero ocorrências de termos de domínio em `orchestrator/` e `communication/`.
- Injeção de `domain` no `Container`, `Coordinator` e `RoleAgentExecutor`.
- 163 testes unitários e de integração aprovados.

---

## Fase 2: Abstração de Provedores (✅ CONCLUÍDA)
- Criação do `CapabilityRegistry`, `ModelCapability` e `ModelSpec` em `core/capabilities.py` para suporte a capacidades heterogêneas (raciocínio, visão, código, ferramentas, etc.).
- Criação da interface base abstrata `Provider` em `core/providers.py` (responsável exclusiva por rede/SDK/CLI, cotas, cooldowns e verificação de saúde).
- Implementação de `GeminiProvider`, `OpenCodeProvider` e `MockProvider`.
- Criação da entidade `Agent` em `core/agent.py` combinando Persona, Habilidades (Skills) e vínculo a Modelo e Provedor.
- Suporte a verificação de capacidades requeridas no `AgentRegistry.select()`.
- Preservação total de retrocompatibilidade com `AgentBackend`.
- 169 testes aprovados (0 regressões).

---

## Fase 3: Motor de Workflow Dinâmico (✅ CONCLUÍDA)
- Criação de `WorkflowDAG`, `WorkflowStage`, `WorkflowContext` e `WorkflowEngine` em `core/workflow.py`.
- Separação estrita entre DAG de Tarefas (subtarefas e arquivos) e DAG de Workflow (etapas de ciclo de vida).
- Validação estrutural de DAG (detecção de ciclos e dependências ausentes) e cálculo de ondas de execução concorrentes.
- Coordinator refatorado para executar etapas via DAG dinâmico (`standard_dag`, `plan_only_dag`, `no_review_dag`, `resume_final_review_dag`).
- Suporte a salto dinâmico de revisão cruzada para tarefas simples/assets (`skip_review` / `category='asset'`), economizando tokens e tempo.
- 176 testes aprovados (0 regressões).

---

## Fase 4: Roteamento Inteligente e Workspace Unificado (✅ CONCLUÍDA)
- **Banco Vetorial Local & RAG:** Criação do `VectorStore` em `memory/vector_store.py` com persistência SQLite e vetorizador denso determinístico (`HashedDenseEmbedding`), permitindo busca semântica veloz e offline sem dependências externas.
- **Context Manager Semântico:** Indexação automática de documentos do workspace e decisões técnicas no `ContextManager`, suportando `search_relevant` e `build_augmented` para injeção contextual precisa no planejamento e execução.
- **Roteamento Inteligente Ciente de Capacidades e Custo:** `IntelligentRouter` em `orchestrator/assignment.py` avalia dinamicamente os modelos registrados no `CapabilityRegistry`, direcionando tarefas de alta complexidade/arquitetura para modelos de raciocínio e tarefas de assets/docs/boilerplate para modelos de melhor custo-eficiência.
- **Métricas e Relatórios de Custo:** Cálculo e agregação de tokens e custos estimados por modelo e agente (`Repository.get_cost_summary`), expostos via `GET /metrics/costs` e `GET /providers`.
- **Workspace Unificado & Dashboard:** Atualização do painel (`dashboard/index.html`) com suporte a múltiplos provedores, badges de capacidades (Reasoning, Code, Vision), tiers de custo, visualização de DAG de etapas, console RAG interativo e relatórios de custo em tempo real.
- 183 testes aprovados (0 regressões).

---

## Próximos Passos (Evolução Contínua)
- Expansão de novos perfis de domínio (Web Fullstack, Data Science, Python Backend).
- Suporte a modelos de visão e geração procedural de assets multimodais.
- Monitoramento distribuído e execução remota de agentes em nuvem.
