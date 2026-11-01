"""Prompts por skill, neutros em relação ao agente (papéis simétricos).

Os dois agentes recebem o MESMO bloco de contexto compartilhado. A tabela de desempate de atribuição
NÃO aparece aqui (fica em config/assignment_rules.json); os prompts só listam as categorias permitidas.
"""
from __future__ import annotations

import json
from typing import Any

from orchestrator.schemas import CATEGORIES, CHECKLIST

AGENT_LABEL = {"gemini": "Gemini 4 Argon High", "deepseek": "DeepSeek V4.1 Flash Free"}
AGENT_PROFILE = {
    "gemini": "mais forte em arquitetura, segurança, lógica de servidor, DataStore e remotes",
    "deepseek": "mais forte em implementação Luau, UI, código mecânico, boilerplate e configuração",
}

COMMON_RULES = """REGRAS GERAIS (valem para todos os agentes):
- Use somente informações presentes no CONTEXTO COMPARTILHADO e nos arquivos reais do workspace. Não invente mecânicas, nomes, números ou sistemas do jogo.
- Se uma informação necessária não existir no contexto, diga explicitamente que ela não foi encontrada.
- Respeite AGENTS.md e as decisões técnicas registradas.
- Não invente APIs do Roblox/Luau. Não declare testes como aprovados se não os executou.
- Não altere arquivos fora dos ARQUIVOS RESERVADOS da sua subtarefa. Se precisar, reporte em "issues".
- Não execute comandos destrutivos (reset, checkout, rm, push) nem altere configurações do repositório.
- Não assuma conexão com o Roblox Studio via MCP: use apenas se o ESTADO MCP indicar conexão ativa para você.
- Responda SOMENTE com o JSON pedido, sem texto fora dele."""

READ_ONLY = "MODO SOMENTE LEITURA: NÃO crie, edite, mova nem apague arquivos nesta etapa. Qualquer alteração invalida sua resposta."


def _other(agent: str) -> str:
    return "deepseek" if agent == "gemini" else "gemini"


def _mcp_line(mcp_status: dict | None, agent: str) -> str:
    if not mcp_status:
        return "ESTADO MCP: nenhuma integração MCP configurada. Trabalhe apenas com arquivos."
    servers = []
    for sid, s in mcp_status.items():
        a = (s.get("agents") or {}).get(agent, {})
        servers.append(f"{sid}: {a.get('state', 'unknown')}")
    return "ESTADO MCP (para você): " + "; ".join(servers)


def _dump(v: Any) -> str:
    return json.dumps(v, ensure_ascii=False)


def plan_prompt(agent: str, goal: str, context_text: str, max_subtasks: int, mcp_status: dict | None) -> str:
    return f"""Você é {AGENT_LABEL.get(agent, agent)} e vai PLANEJAR uma funcionalidade de um projeto Roblox em Luau. {READ_ONLY}
{COMMON_RULES}
{_mcp_line(mcp_status, agent)}

AGENTES DISPONÍVEIS (os dois planejam, implementam e revisam; quem implementa NUNCA revisa o próprio trabalho):
- "gemini": {AGENT_PROFILE['gemini']}.
- "deepseek": {AGENT_PROFILE['deepseek']}.

Divida a SOLICITAÇÃO em 1 a {max_subtasks} subtarefas executáveis, em ordem. Para cada uma, PROPONHA quem implementa
(assigned_to) com justificativa curta. O outro agente poderá aceitar ou contestar uma única vez.
Categorias permitidas: {", ".join(CATEGORIES)}.
{{
  "summary": "resumo do plano",
  "tasks": [
    {{
      "id": "T1",
      "assigned_to": "gemini" | "deepseek",
      "category": "uma das categorias permitidas",
      "assignment_reason": "por que este agente (curto)",
      "title": "título curto",
      "objective": "objetivo verificável",
      "instructions": "passos concretos",
      "context": "trechos/arquivos do contexto que importam para esta tarefa",
      "files": ["caminhos relativos ao workspace que serão criados/alterados"],
      "acceptance_criteria": ["critério verificável"],
      "constraints": ["restrição técnica"],
      "depends_on": ["ids de tarefas anteriores necessárias"]
    }}
  ],
  "decisions": [{{"title": "...", "decision": "...", "rationale": "...", "tags": ["..."]}}],
  "risks": ["..."]
}}
Duas subtarefas NÃO podem listar o mesmo arquivo, a menos que uma dependa da outra (depends_on).
Não informe "reviewer": o revisor é sempre o outro agente.

SOLICITAÇÃO DO USUÁRIO:
{goal}

CONTEXTO COMPARTILHADO:
{context_text}
"""


def respond_assignment_prompt(agent: str, planner: str, proposals: list[dict], goal: str, context_text: str) -> str:
    return f"""Você é {AGENT_LABEL.get(agent, agent)}. {AGENT_LABEL.get(planner, planner)} propôs quem implementa cada subtarefa abaixo.
{READ_ONLY}
Responda em UM ÚNICO turno: aceite ou conteste cada proposta. Conteste apenas com motivo técnico concreto
(ex.: a subtarefa envolve segurança de servidor, ou é UI/código mecânico). Não haverá nova rodada de debate:
contestações são decididas por regras fixas do orquestrador. Quem implementar não revisará o próprio trabalho.
Perfis: gemini = {AGENT_PROFILE['gemini']}; deepseek = {AGENT_PROFILE['deepseek']}.

OBJETIVO GERAL: {goal}
PROPOSTAS: {_dump(proposals)}

Responda SOMENTE com JSON válido:
{{"responses": [{{"task_id": "T1", "decision": "accept" | "counter", "proposed_agent": "gemini" | "deepseek", "reason": "curto"}}]}}

CONTEXTO COMPARTILHADO:
{context_text}
"""


def _feedback_block(feedback: dict | None, cycle: int, reviewer: str) -> str:
    if not feedback:
        return ""
    source = feedback.get("source", "review")
    if source == "automated_checks":
        return f"""
CHECAGENS AUTOMÁTICAS FALHARAM (tentativa de correção {feedback.get('attempt', 1)}) — corrija TODAS as falhas abaixo:
{_dump(feedback.get('failures', []))}
"""
    if source == "human":
        return f"""
DECISÃO HUMANA após escalonamento — siga esta orientação: {feedback.get('note') or '(sem nota)'}
Último parecer do revisor: {_dump(feedback.get('last_review') or {})}
"""
    findings = [f for f in feedback.get("findings", []) if isinstance(f, dict)]
    return f"""
REVISÃO DE {AGENT_LABEL.get(reviewer, reviewer)} (ciclo de ajuste {cycle}) — trate TODOS os itens:
- Veredito: {feedback.get('verdict')}
- Achados com evidência (arquivo:linha / trecho): {_dump([{k: f.get(k) for k in ('file', 'line', 'snippet', 'issue', 'severity')} for f in findings])}
- Alterações solicitadas: {_dump(feedback.get('requested_changes', []))}
- Perguntas (responda em "answers"): {_dump(feedback.get('questions', []))}
Se discordar de um achado, explique em "disputes" com motivo técnico. Achados com evidência válida prevalecem e devem ser tratados;
divergências que persistirem serão escalonadas para decisão humana.
"""


def implement_prompt(agent: str, task: dict, goal: str, context_text: str, feedback: dict | None, cycle: int,
                     previous_results: list[str], mcp_status: dict | None, reviewer: str | None = None) -> str:
    reviewer = reviewer or _other(agent)
    return f"""Você é {AGENT_LABEL.get(agent, agent)} e vai IMPLEMENTAR uma subtarefa de um projeto Roblox em Luau.
Seu trabalho será revisado por {AGENT_LABEL.get(reviewer, reviewer)} e passará por checagens automáticas.
{COMMON_RULES}
{_mcp_line(mcp_status, agent)}

OBJETIVO GERAL: {goal}
SUBTAREFA: {task.get('title')}
OBJETIVO DA SUBTAREFA: {task.get('objective') or task.get('instructions')}
INSTRUÇÕES: {task.get('instructions')}
CONTEXTO ESPECÍFICO: {task.get('context') or '(nenhum)'}
ARQUIVOS RESERVADOS PARA VOCÊ: {_dump(task.get('files', []))}
CRITÉRIOS DE ACEITAÇÃO: {_dump(task.get('acceptance_criteria', []))}
RESTRIÇÕES TÉCNICAS: {_dump(task.get('constraints', []))}
RESULTADOS DE SUBTAREFAS ANTERIORES: {_dump(previous_results)}
{_feedback_block(feedback, cycle, reviewer)}
Inspecione os arquivos antes de editar. Ao terminar, responda SOMENTE com JSON válido:
{{
  "status": "done" | "partial" | "blocked" | "failed",
  "summary": "o que foi feito",
  "files_modified": ["caminhos relativos"],
  "technical_decisions": ["decisão e motivo"],
  "issues": ["problemas encontrados/pendências"],
  "answers": ["respostas às perguntas do revisor, se houver"],
  "disputes": ["discordância com um achado do revisor + motivo técnico, se houver"],
  "tests": ["testes executados e resultado real"],
  "result": "resultado final"
}}

CONTEXTO COMPARTILHADO:
{context_text}
"""


def review_prompt(reviewer: str, implementer: str, task: dict, report: dict, goal: str, context_text: str, cycle: int,
                  max_cycles: int, git_changes: list[dict], checks: dict | None, mcp_status: dict | None,
                  validate_plan: dict | None = None, validation_errors: list[str] | None = None,
                  previous_review: dict | None = None) -> str:
    plan_block = ""
    if validate_plan:
        plan_block = f"""
VALIDAÇÃO DO PLANO: este plano foi feito por {AGENT_LABEL.get(validate_plan.get('planned_by', ''), 'outro agente')} enquanto você
estava indisponível. Além da revisão, avalie se o plano é adequado: preencha "plan_valid" (true/false) e "plan_concerns".
PLANO: {_dump(validate_plan.get('plan'))}
"""
    reform = ""
    if validation_errors:
        reform = f"""
REFORMULAÇÃO OBRIGATÓRIA (única): seus achados anteriores foram DESCARTADOS por evidência inválida:
{_dump(validation_errors)}
Revisão anterior: {_dump(previous_review or {})}
Refaça a revisão citando arquivo, linha e um trecho LITERAL curto copiado do arquivo (até ±5 linhas da linha informada).
Se não houver problema real comprovável, use "approved".
"""
    return f"""Você é {AGENT_LABEL.get(reviewer, reviewer)} e vai REVISAR o trabalho de {AGENT_LABEL.get(implementer, implementer)}. {READ_ONLY}
{COMMON_RULES}
{_mcp_line(mcp_status, reviewer)}

Revise o estado REAL dos arquivos no workspace. Ciclo de revisão {cycle} (máximo de ajustes: {max_cycles}).
CHECKLIST OBRIGATÓRIO (avalie cada item):
1. acceptance_criteria: cada critério de aceitação foi atendido?
2. roblox_api_misuse: há uso incorreto ou inexistente de APIs do Roblox/Luau?
3. edge_case: casos de borda (nil, valores limite, jogador saindo, concorrência, falhas de rede/DataStore)?
4. client_server_security: o servidor confia em dados do cliente? RemoteEvents/RemoteFunctions validam entrada? Lógica sensível só no servidor?

EVIDÊNCIA OBRIGATÓRIA: todo achado precisa de "file" (caminho relativo), "line" (número) e "snippet" (trecho LITERAL curto,
copiado do arquivo, perto da linha). Achado sem evidência verificável é DESCARTADO automaticamente.
Perguntas sobre decisões técnicas vão em "questions" (não precisam de evidência).

OBJETIVO GERAL: {goal}
SUBTAREFA: {_dump(task)}
RELATÓRIO DE QUEM IMPLEMENTOU: {_dump(report)}
ALTERAÇÕES DETECTADAS PELO GIT: {_dump(git_changes)}
CHECAGENS AUTOMÁTICAS: {_dump(checks or {"status": "no_active_checks"})}
{plan_block}{reform}
Responda SOMENTE com JSON válido:
{{
  "verdict": "approved" | "changes_requested" | "needs_clarification" | "rejected",
  "summary": "avaliação",
  "findings": [{{"file": "src/...", "line": 12, "snippet": "trecho literal", "issue": "problema", "severity": "blocker" | "major" | "minor",
                 "checklist": "{'" | "'.join(CHECKLIST)}"}}],
  "requested_changes": ["ajustes concretos (se changes_requested)"],
  "questions": ["perguntas sobre decisões técnicas (se needs_clarification)"],
  "rejection_reason": "justificativa (obrigatória se rejected)",
  "decisions": [{{"title": "...", "decision": "...", "rationale": "..."}}],
  "plan_valid": true | false | null,
  "plan_concerns": ["..."]
}}
"changes_requested" e "rejected" exigem pelo menos um achado com evidência. Use "rejected" apenas quando a abordagem for
inadequada e não corrigível com ajustes.

CONTEXTO COMPARTILHADO:
{context_text}
"""


def final_review_prompt(agent: str, goal: str, subtasks: list[dict[str, Any]], context_text: str,
                        self_authored: list[str] | None = None, watched_files: list[str] | None = None,
                        validation_errors: list[str] | None = None, previous_review: dict | None = None) -> str:
    own = ""
    if self_authored:
        own = f"""
ATENÇÃO: você mesmo implementou {_dump(self_authored)}. Seus achados sobre elas contam e bloqueiam, mas sua aprovação NÃO
conta como cobertura dessas subtarefas (elas já foram revisadas pelo outro agente). Não as aprove por conta própria.
"""
    reform = ""
    if validation_errors:
        reform = f"""
REFORMULAÇÃO OBRIGATÓRIA (única): seus achados anteriores foram DESCARTADOS por evidência inválida:
{_dump(validation_errors)}
Revisão anterior: {_dump(previous_review or {})}
Refaça citando arquivo, linha e trecho LITERAL curto (até ±5 linhas). Se não houver problema comprovável, use "approved".
"""
    return f"""Você é {AGENT_LABEL.get(agent, agent)} e vai fazer a REVISÃO FINAL DE INTEGRAÇÃO do trabalho abaixo. {READ_ONLY}
{COMMON_RULES}
Cada subtarefa JÁ foi revisada individualmente pelo outro agente. NÃO re-revise cada subtarefa isoladamente.
FOQUE NA INTEGRAÇÃO ENTRE AS SUBTAREFAS:
1. interfaces: assinaturas/retornos usados de um módulo para outro batem?
2. remotes: RemoteEvents/RemoteFunctions criados e consumidos com o mesmo nome e os mesmos argumentos, validação no servidor?
3. requires: caminhos de require existem e apontam para o módulo certo?
4. estado compartilhado: dados compartilhados entre módulos (tabelas, DataStore, atributos) são consistentes, sem duplicação?
5. nomes: nomes de instâncias, chaves e eventos coerentes entre as partes?
{own}
EVIDÊNCIA OBRIGATÓRIA: todo achado precisa de "file", "line" e "snippet" (trecho LITERAL curto, ±5 linhas). Sem evidência = descartado.
OBJETIVO: {goal}
SUBTAREFAS: {_dump(subtasks)}
ARQUIVOS DO TRABALHO: {_dump(watched_files or [])}
{reform}
Responda SOMENTE JSON:
{{"verdict": "approved" | "changes_requested" | "rejected", "summary": "...",
  "findings": [{{"file": "src/...", "line": 1, "snippet": "trecho literal", "issue": "problema de integração", "severity": "blocker" | "major" | "minor"}}],
  "requested_changes": ["..."], "rejection_reason": ""}}
"changes_requested" e "rejected" exigem pelo menos um achado com evidência.

CONTEXTO COMPARTILHADO:
{context_text}
"""
