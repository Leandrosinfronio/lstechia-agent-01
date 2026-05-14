# LS.IA Agent 01 - LinkedIn

Painel local para operar um agente assistido de candidaturas no LinkedIn, com FastAPI, Playwright, Chrome via CDP e interface web em tempo real.

## Recursos

- Painel em `http://127.0.0.1:8001`
- Logs em tempo real
- Contadores de vagas analisadas e candidaturas
- Modo assistido com destaque visual, pausa e cursor `LS`
- Integração opcional com Ollama para avaliar compatibilidade
- Perfil do Chrome isolado para manter login localmente

## Configuracao

1. Crie e ative um ambiente virtual.
2. Instale as dependencias:

```bash
pip install -r requirements.txt
```

3. Copie os arquivos de exemplo:

```bash
copy .env.example .env
copy perfil.example.json perfil.json
```

4. Edite `.env` e `perfil.json` com seus dados locais.
5. Abra o Chrome em modo debug:

```bat
abrir_chrome_debug.bat
```

6. Inicie o painel:

```bash
uvicorn app:app --host 127.0.0.1 --port 8001
```

## Privacidade

Este repositorio ignora arquivos com dados sensiveis ou locais:

- `.env`
- `perfil.json`
- perfis do Chrome
- logs
- historico de vagas aplicadas
- `venv`
- `node_modules`

Antes de publicar, confira com:

```bash
git status --short
git ls-files
```
