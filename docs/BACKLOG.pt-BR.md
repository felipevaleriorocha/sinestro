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

## Cenário 1: orquestrador completo contra cada agente sozinho (2026-11-02, 1 amostra)

Mesma tarefa em todos os braços: três módulos Luau pequenos (`Stack`, `Queue` e `Collections`, que depende dos outros dois), sem anotações de tipo. Pasta git nova por execução, mesmos modelos (`gemini-3.8-flash-high`, `deepseek-v4.1-flash-free`). O `agy` rodou com `--dangerously-skip-permissions` **somente neste benchmark** (veja o achado 1). Uma amostra por braço: trate como uma anedota forte, não como estatística.

| | Orquestrador, modo completo | `agy` sozinho | OpenCode sozinho |
|---|---|---|---|
| Tempo total | 544 s | 94 s | 21 s |
| Chamadas | 10 (Gemini 6, DeepSeek 4) | 1 | 1 (6 passos de ferramenta) |
| Tokens entrada / saída | Gemini 291k / 42k, DeepSeek 5k / 2k | 129k (118k de cache) / 7k | ~81k (com cache) / 1k |
| Custo estimado | US$ 0,57 (estimativa) | n/d | modelo gratuito |
| Intervenções humanas | 0 | 0 | 0 |
| Resultado | 3 arquivos, 16/16 na API, revisão final `passed` | 3 arquivos, 16/16 | 3 arquivos, 16/16 |
| `selene` | 0 erros (o agente adicionou `--# selene: allow(undefined_variable)`) | 2 erros (`script` indefinido) | 2 erros (`script` indefinido) |

Li os nove arquivos: a lógica é equivalente e correta em todos os braços (`pop`/`peek`/`dequeue` em estrutura vazia devolvem `nil`, `reverse` monta uma lista nova por meio de um `Stack` e não altera a entrada, cada módulo retorna a sua tabela).

**Lendo contra a regra de decisão** (≤ 1,5× o tempo e ≤ 2× as chamadas do melhor braço solo, numa feature): o orquestrador levou cerca de 26× o tempo do OpenCode sozinho (5,8× o do `agy` sozinho) e 10× as chamadas, para a mesma qualidade. Numa tarefa tão pequena ele não se paga. Isso ainda não diz nada sobre trabalho mais difícil ou arriscado, onde a revisão cruzada pode pegar defeitos reais; aqui não havia nenhum para pegar.

Para onde foram os 544 s: Gemini ≈ 516 s (plano 99 s, implementação do `T3` 243 s + 63 s de correção, três revisões de 19–39 s, revisão final 47 s) e DeepSeek ≈ 56 s (duas implementações de 14 s, uma revisão de 19 s e uma resposta de atribuição de 9 s). A subtarefa atribuída ao Gemini custou mais do que tudo o que o DeepSeek fez.

### Achados

1. **O `agy` em modo headless não termina uma implementação sem `--dangerously-skip-permissions`.** No piloto foi negado 3 vezes em 3: um comando de shell (solo, modo `accept-edits`), uma ação de MCP (como implementador dentro do orquestrador) e um comando (revisão, numa execução anterior). A chamada solo chegou a devolver `status: SUCCESS` com resposta vazia. Este é o limite real de confiabilidade do canal `agy` hoje.
2. **O `opencode run` pega o diretório de trabalho da variável de ambiente `PWD`, não do diretório do processo.** Iniciado de um shell cujo `PWD` era o repositório do projeto, ele escreveu arquivos lá. O orquestrador não é afetado (passa `location.directory` explicitamente), mas qualquer script que chame `opencode run` precisa definir `PWD`.
3. **O falso positivo do `selene` voltou** (`script` não está definido, o global do Roblox). O implementador o silenciou com um comentário `allow` na linha. A correção é a mesma do achado anterior: configurar o `selene` com a biblioteca padrão do Roblox.
4. **A política de atribuição pesa mais do que o número de chamadas.** Mandar a implementação para o DeepSeek e deixar o Gemini no plano e na revisão teria cortado cerca de metade do tempo nesta tarefa.

### Próximo

Rodar o cenário de failover (cota esgotada no meio da tarefa). Opcionalmente repetir o cenário 1 com uma feature mais difícil e propensa a defeitos, onde a revisão tenha o que encontrar, e com mais 2 repetições.
