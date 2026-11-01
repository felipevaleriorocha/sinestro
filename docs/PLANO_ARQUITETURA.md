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
- Centralização da liberação de locks (retenção obrigatória caso não haja confirmação de encerramento do processo externo).
- Ciclo de vida estrito do `stage_log`, registando o status exato da base de dados e impedindo mascaramento de falhas.
- Injeção exclusiva da política de dependências (`config/dependency_policy.json`).
- Resolução de problemas de encoding e consolidação de testes rigorosos do ciclo topológico e de falhas estruturais (154 testes aprovados).

---

## Fase 1: Extração Passiva do Domínio (Em Breve)
**Objetivo:** Desacoplar todo o conhecimento específico de Roblox/Luau do núcleo do orquestrador, mantendo o comportamento atual 100% inalterado. Sem chamadas a novos modelos ou implementações de abstração nesta etapa.

### 1. Estrutura de Diretórios
```text
├── core/
│   ├── config.py
│   ├── domain.py                 # NOVO: Interface base DomainProfile
├── domains/                      # NOVO: Diretório de domínios
│   ├── __init__.py
│   └── roblox/
│       ├── __init__.py
│       ├── profile.py            # NOVO: Implementação de DomainProfile para Roblox
│       ├── prompts.py            # MOVIDO: de orchestrator/prompts.py
│       └── config/               # MOVIDO: Arquivos JSON específicos do domínio
```

### 2. O Contrato DomainProfile
Criação da interface abstrata `core/domain.py` que o Container passará a injetar no `Coordinator` e no `RoleAgentExecutor` (A2A).
O perfil deverá fornecer:
- Prompts geradores (`get_prompts()`).
- Regras de atribuição (`load_assignment_rules()`).
- Checagens permitidas (`load_checks_config()`).
- Fontes de contexto (`load_context_sources()`).

### 3. Critérios de Aceitação da Fase 1
- Pesquisas globais no código por "Luau", "Roblox", "DataStore" e "RemoteEvent" na pasta `orchestrator/` e `communication/` devem retornar zero resultados.
- Todos os ficheiros JSON de configuração (`checks.json`, `assignment_rules.json`, `context_sources.json`) serão lidos pelo domínio e entregues ao Core já analisados (parsed).
- Nenhum comportamento do fluxo Roblox atual poderá ser alterado.
- Os 154 testes do baseline continuam a passar perfeitamente.

---

## Fases Futuras

### Fase 2: Abstração de Provedores
- Desacoplar `AgentBackend`.
- Criar a entidade `Provider` (lida apenas com chamadas de rede/SDK, cotas e limites).
- Criar a entidade `Agent` (Persona + Skills + Associação a um Model/Provider).
- Preparar a interface para suportar capacidades heterogéneas (ex: modelos puramente visuais vs. modelos de raciocínio).

### Fase 3: Motor de Workflow Dinâmico
- Refatorar o `Coordinator` para interpretar um DAG de etapas genéricas, abandonando o fluxo hardcoded (Planeamento -> Atribuição -> Implementação -> Revisão).
- Permitir fluxos que saltem a revisão (ex: tarefas de geração de assets simples).

### Fase 4: Roteamento Inteligente e Workspace Unificado
- Implementar banco vetorial local (SQLite VSS / ChromaDB) para o Context Manager (RAG).
- Orquestrador seleciona agentes automaticamente pelo melhor rácio custo/capacidade.
- Renovar o painel (`dashboard/index.html`) para suportar múltiplos projetos, acompanhamento unificado e relatórios de custo multiplataforma.
