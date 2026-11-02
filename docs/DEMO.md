# Demo guiada (modo mock)

Roteiro de ~45 segundos para mostrar o SINESTRO funcionando sem `agy`, OpenCode ou tokens. Serve tanto para explorar o projeto quanto para gravar o GIF do README.

> Nada neste modo é integração real: os agentes são simulados (`agents/mock_adapters.py`). O que a demo mostra é o **fluxo de orquestração** (plano, DAG, atribuição, revisão cruzada, revisão final).

## 1. Preparar

```powershell
py -3 -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
Copy-Item .env.example .env
```

No `.env`, defina:

```
PROJECT_ROOT=C:\caminho\para\uma\pasta\vazia
ORCH_MOCK_AGENTS=true
```

## 2. Subir

```powershell
.\.venv\Scripts\python run_server.py
```

- Painel: <http://127.0.0.1:8787/dashboard/>
- API: <http://127.0.0.1:8787/docs>

## 3. Enviar uma tarefa

Pelo painel, ou:

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

Ferramentas gratuitas: [ScreenToGif](https://www.screentogif.com/) (Windows) ou [LICEcap](https://www.cockos.com/licecap/). Salve em `docs/media/demo.gif` (mantenha abaixo de ~5 MB) e descomente a linha de imagem no topo do `README.md`.
