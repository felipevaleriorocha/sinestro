# Changelog

## 0.5.1 — Gestão de Permissões e Aprovação Humana Prévia
- **Auto-aprovação configurável (`AGY_AUTO_APPROVE_PERMISSIONS`):** Suporte à flag `--dangerously-skip-permissions` no CLI do `agy`. Por padrão (`false`), o agente não executa ações com permissões elevadas sem autorização.
- **Aprovação Prévia (`requires_approval` / `requires_permission`):** Subtarefas que exigem comandos no sistema ou privilégios de execução são pausadas em `needs_human` antes do início, evitando recusas não interativas.
- **Recuperação de Erro de Permissão:** Quando uma chamada falha com permissão negada pelo `agy` (`AgentPermissionError` / `permission_denied`), a subtarefa transiciona para `needs_human` com aviso detalhado no painel em vez de quebrar o workflow.
- **Fluxo de Decisão no Painel:** Ao clicar em `approve` para uma subtarefa sem resultado prévio, o sistema concede permissão (`allow_permissions=True`), retorna para `pending` e injeta a autorização ao retomar (`POST /tasks/{id}/resume`).
- **Identidade Visual:** Atualização do logotipo da DeepSeek no painel com o ícone oficial (baleia azul).
- **Iniciador (`start.bat`):** Caminho relativo dinâmico (`%~dp0`) para inicialização independente do diretório de instalação.

## 0.5.0 — Evolução Arquitetural: Workspace Multi-Provider
- **Fase 0 (Estabilização do Core):** Centralização estrita de retenção de locks em caso de processo sem confirmação de encerramento (timeout), ciclo de vida íntegro de `stage_log` e injeção isolada da política de dependências (`dependency_policy.json`).
- **Fase 1 (Extração de Domínio Passivo):** Interface `DomainProfile` (`core/domain.py`) e domínio `roblox` desacoplados do núcleo; Core 100% agnóstico a linguagens e frameworks.
- **Fase 2 (Abstração de Provedores):** Entidades `Provider`, `ModelSpec`, `ModelCapability` e `CapabilityRegistry` separadas de agentes e backends concretos.
- **Fase 3 (Motor de Workflow Dinâmico):** `WorkflowDAG` e `WorkflowEngine` com ordenação topológica por ondas, execução flexível e suporte a saltos de etapas (ex.: sem revisão cruzada para assets).
- **Fase 4 (Roteamento Inteligente e Workspace Unificado):** RAG local híbrido via SQLite (`VectorStore`), indexação automática do workspace, roteador de tarefas por custo/capacidade e relatório financeiro consolidado no painel.

## 0.4.2
- Contratos aceitam `null` em campos opcionais (antes uma revisão aprovada com `"rejection_reason": null` era tratada
  como não estruturada e gastava uma revisão extra).
- Reformulação distingue erro de FORMATO de erro de EVIDÊNCIA; sem achado sintético "não retornou JSON".
- Painel: atualização sem perder rolagem/seleção (`setHTML`); `docs/FRONTEND.md`.
- Checagens recebem os caminhos com maiúsculas originais.
- `Read-EnvFile` aceita caminho relativo.
- Cards e prompts sem o nome do modelo fixo (o modelo vem do `.env`).

## 0.4.1 — revisão final sem furos
- `final_review_state` sempre gravado (passed, disabled, skipped, failed, changes_requested, rejected, unverified,
  invalidated, not_reached, accepted_by_human, rejected_by_human) e decisão centralizada na função pura `final_status()`.
- Novo status `completed_unreviewed`: subtarefas aprovadas, mas sem revisão final. Nunca vira `completed` limpo.
- Revisor da revisão final nunca é quem implementou tudo; no caso misto, Gemini com regra assimétrica e
  `self_authored_subtasks` + cobertura por subtarefa no resultado.
- Revisão final focada em integração, com evidência, reformulação única, comparação de hash e modo somente leitura.
- Retomada de `completed_unreviewed`/`needs_human` final roda só a revisão final (tentativa + snapshot; evento se o
  workspace mudou). `POST /tasks/{id}/final-review/decision` (accept|reject). Veredito negativo -> `needs_human`.
- Painel: selo "revisão final desligada", aviso "sem revisão final", seção da revisão final. Schema 5 (backup `.bak-v4`).

## 0.4.0 — papéis simétricos
- Os dois agentes têm as mesmas skills A2A: `plan_feature`, `respond_assignment`, `implement_task`,
  `review_implementation` e `final_review`. **Quem implementa nunca revisa**: o revisor é calculado como o outro agente,
  com validação no contrato (`AssignmentDecision`), no repositório e no coordenador (`implemented_by`).
- Atribuição em 2 turnos: proposta no plano, aceite/contestação em uma chamada e desempate determinístico por
  `config/assignment_rules.json` (`default_agent` configurável). Gemini sem cota -> DeepSeek assume (`quota_fallback`).
- Checagens automáticas plugáveis (`config/checks.json`, lista fixa, sem shell, desligadas por padrão). Falha volta ao
  implementador sem revisão de LLM; falha idêntica escala na hora.
- Revisão com checklist e evidência (`ReviewFinding`: arquivo, linha e trecho a ±5 linhas). Uma única reformulação;
  depois disso, `review_unverified` -> `needs_human`. Alteração de arquivos durante a revisão invalida o veredito.
- Contadores separados: `review_cycles`, `check_fix_attempts` e `impl_attempts` (teto global).
- Estados novos: subtarefa `awaiting_review` / `needs_human`; workflow `needs_human` / `paused`.
  `POST /tasks/{id}/subtasks/{sid}/decision` + `resume`.
- Plano feito por fallback é validado pelo Gemini na primeira revisão. Dependentes só iniciam com a dependência aprovada.
  Pausa ao atingir `MAX_BLOCKED_SUBTASKS`.
- Métricas por workflow: contestações, quem contestou, vencedor e regra do desempate, quantas vezes a negociação mudou
  o resultado, e chamadas por agente/skill.
- Adaptadores: agy com `--mode plan` nas skills somente leitura e `--add-dir` para prompts em arquivo (ambos detectados
  pelo `--help`); cooldown de cota também no Gemini; OpenCode com agente `Plan` para revisão, em sessão separada.
- Banco schema 4 com backup automático `orchestrator.db.bak-v<N>`.
- Painel: colunas Implementou / Revisou / Motivo, contadores, botões de decisão humana e aviso "nenhuma checagem ativa".
- `run_server.py` com `access_log=False` e `Iniciar Orquestrador.bat` (commit separado).

## 0.3.0
A2A com o SDK oficial, adaptadores AGY/OpenCode 2.x, revisão pelo Gemini, memória compartilhada, reservas de arquivos,
preparação MCP, painel e testes (detalhes em docs/AUDITORIA_v0.2.md).
