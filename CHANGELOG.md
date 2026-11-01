# Changelog

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
