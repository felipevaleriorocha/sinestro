# Demo guiada (modo mock)

[English](DEMO.md)

Roteiro de ~45 segundos para mostrar o SINESTRO funcionando sem `agy`, OpenCode ou tokens. Serve tanto para explorar o projeto quanto para gravar o GIF do README.

> Nada neste modo é integração real: os agentes são simulados (`agents/mock_adapters.py`). O que a demo mostra é o **fluxo de orquestração** (plano, DAG, atribuição, revisão cruzada, revisão final).

## 1. Preparar

```powershell
if (!(Test-Path .venv)) { py -3 -m venv .venv }
.\.venv\Scripts\python -m pip install -r requirements.txt
if (!(Test-Path .env)) { Copy-Item .env.example .env }
```

> Use `python -m pip` em vez de `pip.exe`: se a pasta do projeto foi copiada de outro lugar, o launcher `pip.exe` do `.venv` continua apontando para o caminho antigo e falha com "Fatal error in launcher". Se isso acontecer, apague o `.venv` (com o servidor parado) e recrie.
> O `if (!(Test-Path .env))` evita sobrescrever um `.env` que você já tenha configurado.

No `.env`, defina:

```
ORCH_MOCK_AGENTS=true
```

O `PROJECT_ROOT` é opcional. Deixe vazio e escolha o projeto pelo painel (passo 3).

## 2. Subir

```powershell
.\.venv\Scripts\python run_server.py
```

- Painel: <http://127.0.0.1:8787/dashboard/>
- API: <http://127.0.0.1:8787/docs>

## 3. Escolher um projeto e enviar uma tarefa

No primeiro uso, o painel pede um projeto: **Abrir pasta…** abre o explorador de arquivos para você escolher um projeto existente, e **Criar novo projeto** cria uma pasta em `~/Documents/Sinestro`. A escolha é gravada no `.env`.

Depois envie a tarefa pelo painel, ou:

```powershell
curl.exe -X POST http://127.0.0.1:8787/tasks -H "Content-Type: application/json" -d "{\"goal\": \"Criar um modulo hello-world\", \"mode\": \"full\"}"
```

## 4. O que mostrar no GIF (sugestão de roteiro)

1. **0-5 s:** painel vazio com os cards dos dois agentes.
2. **5-15 s:** enviar o objetivo; aparece o plano com subtarefas.
3. **15-30 s:** DAG e atribuição; mostrar que quem implementa não é quem revisa.
4. **30-40 s:** revisão cruzada e checagens automáticas.
5. **40-45 s:** `final_review_state: passed` e status `completed`.

## 5. Gravar

Ferramentas gratuitas: [ScreenToGif](https://www.screentogif.com/) (Windows) ou [LICEcap](https://www.cockos.com/licecap/). Salve em `docs/media/demo.gif` (mantenha abaixo de ~5 MB) (já referenciado no topo dos READMEs).
