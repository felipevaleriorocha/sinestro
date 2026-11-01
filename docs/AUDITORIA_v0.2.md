# Auditoria do starter v0.2 (resumo)

Originais em `backup_v0.2/`. Principais problemas corrigidos na v0.3:
1. `OPENCODE_AGENT=build` era enviado sempre; agora o agente só é enviado se existir em `/api/agent`.
2. A `variant` era enviada sem conferir `/api/model`.
3. As rotas de sessão do OpenCode estavam hardcoded; agora ficam em `config/opencode_routes.json`, conferidas pela OpenAPI do servidor.
4. Quota excedida não tinha tratamento; agora há cooldown persistido.
5. As flags do agy não eram validadas; agora só são usadas flags confirmadas no `agy --help`.
6. Windows: `agy.cmd` corrompia prompts com aspas e quebras de linha, e o timeout deixava processos órfãos.
7. As conexões SQLite não eram fechadas, e tarefas em `running` ficavam assim após reiniciar.
8. `ConvertFrom-StringData` (PowerShell) e python-dotenv corrompiam caminhos com `\`; agora o parser é literal.
9. Não havia A2A; ele passou a usar o SDK oficial.
