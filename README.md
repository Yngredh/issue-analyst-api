# issue-analyst-api

Recebe issues do GitHub, valida e encaminha para o processo de triagem.

## Pré-requisitos

- [uv](https://docs.astral.sh/uv/) (instala o Python 3.12 automaticamente, se necessário)

## Comandos

```bash
uv sync                                   # instala as dependências
uv run pytest                             # roda os testes
uv run python -m issue_analyst_api.main   # inicia o processo
```

Copie `.env.example` para `.env` para sobrescrever as variáveis de ambiente.

## Documentação

Veja [docs/README.md](../docs/doc-reference.md), que aponta para o índice dos documentos do projeto.
