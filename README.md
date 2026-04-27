# Binance Futures Bot

Bot de cycling para Binance Futures com ordens limitadas.

## Como funciona

- **LONG**: Compra no preço atual → Vende a +X% → Compra de novo no preço original → repete
- **SHORT**: Vende no preço atual → Compra a -X% → Vende de novo no preço original → repete
- 4 slots independentes, cada um com sua própria API key
- Sem Stop Loss
- Ordens limitadas (GTC)

## Deploy no Railway

1. Suba este projeto no GitHub
2. No Railway, clique "New Project" → "Deploy from GitHub repo"
3. Selecione o repositório
4. Railway detecta o Dockerfile automaticamente
5. Acesse a URL gerada e configure os slots

## Rodar localmente

```bash
pip install -r requirements.txt
python app.py
```

Acesse http://localhost:5000

## Via AnyDesk

1. Instale Python 3.12+ na máquina remota
2. Copie a pasta do projeto
3. `pip install -r requirements.txt`
4. `python app.py`
5. Abra o navegador em http://localhost:5000
