# Backlog e primeiras medições com agentes reais

[English](BACKLOG.md)

Origem: auditoria externa do commit `7de4854` (2026-11), reconferida contra a `main` em `d7876b4` em 2026-11-02, mais a primeira execução com agentes reais. A auditoria é uma opinião de fora: cada item foi verificado no código antes de ser aceito. Status: **Corrigido**, **Aberto**, **Decisão** (depende do dono) e **Não verificado**.

## Itens da auditoria

| # | Item | Status | Notas |
|---|---|---|---|
| 1 | CORS com `*` + credenciais e `TrustedHost` com `*` | **Corrigido** (`7ffffc5`) | `api/main.py` usa lista explícita de origens e sem host `*`. `test_cors_middleware_headers` garante que uma `Origin` estranha não é devolvida. Em aberto: token para métodos que alteram estado (`POST/PATCH/DELETE`) e teste que rejeite `Host` estranho. |
| 2 | Gate do canal Gemini e vazamento da chave | **Corrigido** (em boa parte) | `GEMINI_CHANNEL_ACK_RISK` existe, a chave vai em `x-goog-api-key` e `Settings.secrets()` a inclui. Aberto: o canal `api` recusar `implement_task` (ele só devolve texto e não edita arquivos). |
| 3 | Retry de stdout vazio | **Corrigido** | `"não retornou saída"` agora dispara retry em `agents/gemini_adapter.py`. Ainda sem teste de regressão do retry. |
| 4 | Revisão final dispensada gravada como `passed` | **Corrigido** | O modo simples grava `skipped_by_policy` (`orchestrator/final_review.py`). |
| 5 | Fonte única de versão | **Corrigido** | `core/version.py`; versão 0.5.2. |
| 6 | `resume` não pode zerar contadores | Aberto | A mutação que zera os contadores no resume sobrevive à suíte. Falta teste de regressão. |
| 7 | Cancelar só alcança `implement_task` | Aberto | `last_a2a_task_id` só é gravado na implementação, então plano, revisão e revisão final em andamento seguem rodando após cancelar. O botão de cancelar só aparece para tarefas ativas. |
| 8 | Poda de 30 dias apaga também `decisions` aceitas | Aberto | `Repository.prune_inactive_workflows` apaga decisões; preservar as aceitas (alimentam o contexto). |
| 9 | Roteamento: só roda na contestação, amostras poluídas | Aberto | Consultar o placar também quando a proposta é aceita; excluir `blocked`/`canceled`/`skip_review` e contagem dupla no resume; subir o mínimo para ~8–10 amostras; medir só o tempo de implementação; exploração em categorias de baixo risco. |
| 10 | Badge de cota usa orçamentos de tokens inventados | Aberto | `agent_registry.py` assume 1,2M/500k tokens por 24 h; a cota real é semanal. Mostrar cooldown real e contadores semanais locais, rotulados "estimado". |
| 11 | Sem progresso ao vivo (`/activity`, stream-json/SSE) | Aberto | O painel continua inferindo o que cada agente faz. |
| 12 | `raw_text` ainda chega ao revisor | Aberto | O `model_dump()` do relatório de implementação o inclui. |
| 13 | Termos de uso para orquestrar o `agy` | **Decisão** | As respostas do fórum se contradizem; o código nunca toca em tokens nem credenciais. Decidir antes de usar o `agy` dentro do orquestrador a longo prazo. Não é aconselhamento jurídico. |
| 14 | `agy -p` devolvendo 0 bytes sem TTY (Windows) | Não observado | Não ocorreu na execução real abaixo (7 chamadas, 0 vazias). Continuar acompanhando a taxa de respostas vazias. |

Fora do escopo agora (segundo a auditoria): mais provedores, RAG, execução paralela, ACP, generalizar para N agentes e estimativas em dólar.

## Protocolo de benchmark (ainda não executado, exceto o primeiro passo)

Quatro braços — orquestrador completo, orquestrador simples, `agy` sozinho e OpenCode sozinho — em 3 tarefas mecânicas e 1 feature de 2–3 subtarefas, 3 repetições cada. Registrar chamadas por agente, tempo total, intervenções humanas e passou/falhou.

Defina os limiares *antes* de rodar. Sugestão: manter o orquestrador se, na feature, igualar o melhor braço solo com ≤ 1,5× o tempo e ≤ 2× as chamadas; nas mecânicas, ≤ 1,2× as chamadas do melhor solo; autonomia ≥ 70 %; respostas vazias ou fora do formato ≤ 5 % após o retry. O placar de roteamento só se paga se o first-pass melhorar depois de ~30 tarefas limpas.

## Primeira execução real (2026-11-02)

Agentes reais (Gemini via `agy`, DeepSeek via OpenCode) numa pasta git descartável, modo `simple`, auto-aprovação **desligada**, `selene` ativo. Duas tarefas Luau minúsculas, uma amostra cada — isto é uma anedota, não um benchmark.

| | Execução 1: `Greeter.luau` (com tipos) | Execução 2: `Farewell.luau` (sem tipos) |
|---|---|---|
| Resultado | `needs_human` | `completed`, aprovada de primeira |
| Tempo total | 137 s | 111 s |
| Chamadas | 4 (plano, implementação, implementação de novo, revisão) | 3 (plano, implementação, revisão) |
| Revisão final | não alcançada | `skipped_by_policy` |

Totais das 7 chamadas: Gemini 4 chamadas (100,5k tokens de entrada / 15,0k de saída, estimativa de US$ 0,20 — é uma estimativa), DeepSeek 3 chamadas (modelo gratuito). O plano do Gemini levou 40–58 s, a implementação do DeepSeek 15–41 s e a revisão do Gemini 21–32 s. 0 respostas vazias, 0 erros de cota.

### Achados novos

1. **Falso positivo da checagem fez o agente piorar o código.** Num projeto vazio, o `selene` leu a anotação Luau `greet(name: string): string` como 6 erros de sintaxe, devolveu a tarefa ao implementador, e ele "corrigiu" apagando os tipos que a tarefa pedia. Uma checagem com falha não pode empurrar o agente abaixo do requisito: configurar o `selene` para Luau/Roblox (`selene.toml`, `std = "roblox"`), ligar o `luau-analyze`, ou pular a checagem quando o projeto não tem configuração de linter.
2. **Revisão negada em modo headless.** Com a auto-aprovação desligada, a revisão somente leitura do Gemini tentou rodar um comando e o `agy` recusou (`permission_denied`); 1 de 2 revisões. O sistema escalou para `needs_human` corretamente, mas o custo foi uma tarefa inteira. Opções: permitir comandos de leitura na skill de revisão, tentar de novo uma vez, ou avisar claramente que o sandbox precisa de auto-aprovação.
3. **A entrada do Gemini tem ~25k tokens por chamada** em tarefas minúsculas (sobrecarga de contexto do CLI), então o que pesa no custo é o número de chamadas ao `agy`, não o tamanho da tarefa. O modo simples (3 chamadas) é o padrão certo para trabalho mecânico.
4. **O modo simples funciona como projetado:** 3 chamadas, sem negociação, sem revisão final, com um `skipped_by_policy` honesto.

### Ainda a medir

Linhas de base solo (`agy`, OpenCode), modo completo numa feature de 2–3 subtarefas, failover de cota no meio da tarefa e `/metrics/routing-stats` depois de ~20–30 tarefas limpas.
