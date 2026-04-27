# 🤖 Binance Scaler — Documentação Completa

Um bot de trading automatizado para Binance Futures com painel de monitoramento em tempo real. Ele executa uma estratégia de scalping baseada em grade com ciclos perpétuos de re-entrada, suporta múltiplos pares de trading simultaneamente e oferece uma interface ao vivo — tudo a partir de um servidor Flask que você pode hospedar localmente ou na nuvem via Railway.

---

## Índice

1. [Como Funciona — Visão Geral](#como-funciona)
2. [Estratégia de Trading Principal](#estratégia-de-trading-principal)
3. [Funcionalidades do Painel](#funcionalidades-do-painel)
4. [Referência de Configuração](#referência-de-configuração)
5. [Instalação e Execução Local](#instalação-e-execução-local)
6. [☁️ Deploy no Railway (Recomendado)](#️-deploy-no-railway-recomendado)
7. [Arquitetura do Sistema](#arquitetura-do-sistema)

---

## Como Funciona

Ao iniciar o bot (`python app.py`), um servidor web é iniciado na porta **3000**. Você acessa o painel pelo navegador. O motor de trading roda inteiramente em segundo plano como um processo Python. Toda a comunicação em tempo real entre o navegador e o servidor acontece via **WebSockets (Socket.IO)** — sem necessidade de atualizar a página.

**Fluxo de Dados:**
```
API da Binance (REST + WebSocket)
        ↓
  Motor de Trading (bot_engine.py)
        ↓
  Flask / Socket.IO (app.py)
        ↓
  Painel no Navegador (dashboard.html + app.js)
```

Um **worker de plano de fundo** inicia imediatamente ao abrir o bot — mesmo antes de clicar em "Iniciar". Esse worker:
- Busca preços de mercado ao vivo para todos os símbolos configurados a cada ~1 segundo
- Busca os limites máximos de alavancagem da exchange para cada símbolo
- Busca as regras de precisão da exchange (step size, tick size) para formatação correta de ordens
- Emite dados de preço e conta ao vivo para todas as janelas do navegador conectadas

---

## Estratégia de Trading Principal

O bot implementa um **sistema de scalping perpétuo em grade para contratos futuros**. Veja o que acontece passo a passo:

### 1. Entrada Inicial
Ao iniciar o bot, ele coloca uma **Ordem Limite de Compra** (LONG) ou **Ordem Limite de Venda** (SHORT) no preço `entry_price` configurado. O tamanho total da posição é calculado como:

```
Tamanho da Posição = (trade_amount_usdc × alavancagem) / preço_de_entrada
```

**Exemplo:** `$100 USDC × 95x alavancagem / $9,00 = ~1.055,5 LINK`

> Tanto a quantidade quanto o preço são formatados automaticamente com a precisão exata exigida pela Binance para aquele símbolo, evitando erros de precisão na API.

### 2. Lógica de "Usar Ativos Existentes"
Se você já tem uma posição aberta quando o bot inicia, ele **não abrirá uma nova entrada**. Em vez disso, lê a posição existente e coloca imediatamente a grade de Take Profit sobre ela. Isso permite anexar o bot a uma posição aberta manualmente.

- **Toggle LIGADO (padrão):** Anexa a grade à posição existente — sem nova entrada.
- **Toggle DESLIGADO:** Sempre coloca uma nova ordem de entrada ao preço configurado, mesmo que exista uma posição.

### 3. A Grade de Take Profit (TP)
Após o preenchimento da entrada inicial, o bot coloca imediatamente **múltiplas Ordens Limite de Venda** (LONG) ou **de Compra** (SHORT) distribuídas em níveis de preço. A grade é definida por:

- **`total_fractions`**: Quantos níveis dividem a posição (ex: `8` = 8 ordens de TP, cada uma valendo 12,5% da posição total).
- **`price_deviation`**: A variação percentual de preço entre cada nível (ex: `0,6%`).

**Exemplo de Grade — LONG a $9,00, 8 frações, 0,6% de desvio:**

| Nível | Preço  | % da Posição Vendida |
|-------|--------|----------------------|
| 1     | $9,054 | 12,50%               |
| 2     | $9,108 | 12,50%               |
| 3     | $9,162 | 12,50%               |
| 4     | $9,216 | 12,50%               |
| 5     | $9,270 | 12,50%               |
| 6     | $9,324 | 12,50%               |
| 7     | $9,378 | 12,50%               |
| 8     | $9,432 | 12,50%               |

Cada ordem é uma ordem `LIMIT GTC` ao vivo no livro de ordens da Binance — **não gerenciada localmente**. Seus alvos de lucro persistem mesmo que o bot caia ou perca a conexão.

### 4. O Ciclo Perpétuo de Re-Entrada
É o que torna a estratégia "perpétua". Quando qualquer nível de TP é preenchido:
1. O bot detecta o preenchimento instantaneamente pelo **WebSocket de Dados do Usuário** (sem polling).
2. Coloca imediatamente uma **Ordem Limite de Re-Entrada** no nível de preço logo abaixo do TP preenchido.
3. Quando essa Re-Entrada é preenchida (preço volta), recoloca a ordem de TP naquele nível.
4. Esse ciclo se repete **indefinidamente**, escalpeando o mesmo intervalo de preço repetidamente.

```
Preço sobe:      Entrada preenche → TP Nível 1 preenche → Re-Entrada colocada abaixo
Preço oscila:    Re-Entrada preenche → TP Nível 1 recolocado → Ciclo continua...
```

### 5. Realização de Lucro Móvel (Trailing)
Uma camada opcional sobre a grade. Quando ativada, o bot rastreia o PnL máximo não realizado da posição. Se o PnL cair `trailing_deviation %` do seu pico, o bot **fecha toda a posição a mercado** para garantir o lucro.

- Menor desvio = dispara mais cedo, proteção mais apertada
- Maior desvio = permite que o lucro cresça mais antes de sair

### 6. Segurança de Saldo e Ordens
Antes de colocar qualquer ordem de re-entrada, o bot valida sua margem disponível. Se o saldo for insuficiente, a ordem é ignorada e um aviso é registrado no console — evitando perdas por alavancagem excessiva.

### 7. Fechamento Manual de Posições
Na aba "Posições" do painel, você pode fechar qualquer posição a qualquer momento. O bot:
1. Cancela todas as ordens abertas para aquele símbolo.
2. Envia uma Ordem a Mercado para achatar toda a posição imediatamente.

---

## Funcionalidades do Painel

O painel é uma interface de página única em tempo real dividida em três colunas e um painel inferior.

---

### Coluna Esquerda — Controle Estratégico

#### Seletor de Ativo
Menu suspenso com destaque visual no topo. Selecionar um símbolo diferente **muda todo o painel esquerdo** para exibir e controlar a estratégia daquele símbolo. Cada símbolo tem suas próprias configurações independentes.

#### Exibição de Direção (`LONG/SHORT QUANTIDADE SÍMBOLO`)
Mostra a direção trading ativa, a quantidade calculada em unidades base e o nome do símbolo. Atualiza ao vivo conforme você ajusta o valor do trade ou a alavancagem.

#### Toggle "Usar Ativos Existentes"
- **LIGADO (padrão):** Bot se anexa a qualquer posição aberta existente — sem nova entrada.
- **DESLIGADO:** Bot sempre coloca uma nova ordem de entrada ao preço configurado.

#### Controles de Alavancagem
- **Slider de Alavancagem:** Arraste de 1x até o **máximo permitido pela exchange** para aquele símbolo (ex: 125x para BTCUSDC, 75x para LINKUSDC). O máximo do slider se ajusta automaticamente por símbolo.
- **Marcas Dinâmicas:** Marcadores espaçados (1x, 25%, 50%, 75%, Máx) para sempre saber onde está.
- **Botões de Atalho** (1x, 10x, 20x, 50x, MÁX): Salta instantaneamente para um preset. O botão MÁX exibe o máximo real do símbolo ativo. Presets acima do máximo são automaticamente ocultados.
- **Modo de Margem** (Cruzada / Isolada): Define o modo de margem de futuros aplicado à conta ao iniciar.
- **Margem Prevista:** Mostra quanto de USDC a configuração atual requer como colateral ao preço de mercado atual.

#### Valor de Trade
- **Entrada USDC:** Insira o colateral total em USDC a alocar. Este é sua margem, não o valor nocional.
- **Preço de Entrada:** O preço no qual a ordem limite inicial será colocada.
- **Preço de Mercado ao Vivo:** Exibido abaixo — atualiza a cada segundo, mesmo com o bot parado.

#### Total de Unidades (Nocional)
A quantidade total do ativo que sua posição vai controlar:
```
Total de Unidades = (trade_amount_usdc × alavancagem) / preço_ao_vivo
```

---

### Coluna do Meio — Configurações de Take Profit

#### Toggle de Take Profit
Ativa ou desativa o sistema de grade de TP para o símbolo selecionado.

#### Anel de Disponibilidade
Um indicador circular mostrando qual porcentagem da posição **ainda não foi vendida**. Começa em 100%. Diminui conforme os níveis de TP são preenchidos e aumenta conforme as re-entradas são preenchidas. Dá uma visão ao vivo da atividade da grade.

#### Tabela de Grade de TP
Gerada dinamicamente a partir de suas configurações:
- **Coluna esquerda:** A porcentagem acima/abaixo do preço de entrada para cada nível de TP.
- **Coluna direita:** A quantidade (fração da posição total) vendida naquele nível.
- **Ícone de corrente (🔗):** Indica que o nível está vinculado a uma re-entrada — quando esse TP preencher, uma re-entrada é colocada no nível abaixo.

---

#### Realização de Lucro Móvel (Trailing)

| Controle | O que Faz |
|---|---|
| **Toggle de Ativação** | Liga/desliga o monitoramento de PnL em movimento para este símbolo |
| **Preço Máximo Estimado** | Entrada opcional — insira um preço alvo para simular o lucro esperado antes do trailing disparar |
| **Slider de Desvio (0–10%)** | O quanto o PnL deve cair do pico para acionar o fechamento total |
| **Botões de Atalho** (0,1%, 0,5%, 1%, 2%, 5%) | Valores de desvio rápidos |
| **Lucro Aproximado** | Mostra `$lucro` estimado, `%ganho` e `ROE` em tempo real, usando o Preço Máximo Estimado se definido |

---

### Coluna Direita — Resumo da Conta e Símbolos

#### Resumo da Conta
- **Patrimônio Líquido:** Saldo total em USDC + PnL não realizado de todas as contas ativas.
- **Saldos Individuas:** Cada conta de API é listada com nome, indicador verde/cinza de status e saldo USDC. Sempre visível — mesmo com o bot parado.
- **Contas Ativas:** Contagem de contas de API conectadas e operando.

#### Painel de Símbolos
Lista todos os símbolos configurados. Cada um tem:
- Um **ícone de lixeira** para remover o símbolo da estratégia.
- Um **botão +** para adicionar um novo símbolo à configuração ao vivo sem reiniciar.

---

### Seção Inferior — Posições e Logs

#### Aba Posições
Tabela ao vivo com todas as posições futuras abertas em todas as contas:

| Coluna | Descrição |
|---|---|
| Conta | Qual conta de API detém a posição. Posições manuais têm um badge **"Manual"**. |
| Símbolo | O par de trading |
| Tamanho | Tamanho da posição. Positivo = LONG, Negativo = SHORT |
| Preço de Entrada | Preço médio de preenchimento da ordem de entrada inicial |
| PnL Não Realizado | Lucro/perda ao vivo — verde se positivo, vermelho se negativo |
| Ação | Botão **Fechar** — cancela todas as ordens e fecha a posição a mercado imediatamente |

> Tanto posições gerenciadas pelo bot quanto posições abertas manualmente aparecem aqui. Posições manuais ficam levemente esmaecidas e identificadas com o badge "Manual".

#### Aba Console
Log em tempo real de toda a atividade do bot:
- Colocações de ordens, re-entradas, preenchimentos de TP
- Eventos de conta, atualizações de saldo, mensagens de erro
- Timestamps para cada evento
- Suporte a idiomas (Português / Inglês) conforme configuração

---

## Referência de Configuração

Todas as configurações ficam em **`config.json`**. O painel edita este arquivo em tempo real.

```json
{
  "api_accounts": [
    {
      "name": "Usuário 1",
      "api_key": "SUA_CHAVE_API_BINANCE",
      "api_secret": "SUA_CHAVE_SECRETA_BINANCE",
      "enabled": true
    }
  ],
  "is_demo": true,
  "language": "pt-BR",
  "symbols": ["LINKUSDC", "BTCUSDC"],
  "symbol_strategies": {
    "LINKUSDC": {
      "direction": "LONG",
      "entry_price": 8.565,
      "leverage": 75,
      "margin_type": "CROSSED",
      "price_deviation": 0.6,
      "total_fractions": 8,
      "trade_amount_usdc": 100,
      "use_existing_assets": true,
      "trailing_enabled": true,
      "trailing_deviation": 1.0
    }
  }
}
```

### Referência Completa de Configurações

| Campo | Tipo | Descrição |
|---|---|---|
| `api_accounts[].name` | string | Nome de exibição para esta conta no painel |
| `api_accounts[].api_key` | string | Chave de API da Binance Futures |
| `api_accounts[].api_secret` | string | Chave Secreta da Binance Futures |
| `api_accounts[].enabled` | bool | `true` ativa esta conta para trading |
| `is_demo` | bool | `true` = Testnet da Binance (seguro para testes), `false` = Trading real |
| `language` | string | Idioma do painel: `"pt-BR"` (Português) ou `"en-US"` (Inglês) |
| `symbols` | array | Lista de pares de trading (ex: `["LINKUSDC", "BTCUSDC"]`) |
| **Configurações por símbolo** | | |
| `direction` | string | `"LONG"` (compra inicialmente) ou `"SHORT"` (venda inicialmente) |
| `entry_price` | float | Preço no qual a ordem limite inicial é colocada |
| `leverage` | int | Multiplicador de alavancagem — limitado automaticamente ao máximo da exchange |
| `margin_type` | string | `"CROSSED"` (margem cruzada) ou `"ISOLATED"` (margem isolada) |
| `price_deviation` | float | % de variação de preço entre cada nível de TP (ex: `0.6` = 0,6%) |
| `total_fractions` | int | Número de níveis de TP (ex: `8` divide a posição em 8 partes iguais) |
| `trade_amount_usdc` | float | Margem em USDC a arriscar (colateral, não valor nocional) |
| `use_existing_assets` | bool | `true` = anexa grade à posição existente em vez de abrir nova entrada |
| `trailing_enabled` | bool | Ativa saída por PnL em movimento sobre a grade |
| `trailing_deviation` | float | % de queda do pico de PnL para acionar fechamento total da posição |

> ⚠️ **Atenção:** Todas as alterações feitas no painel são salvas em `config.json` imediatamente. Se o bot estiver rodando, as mudanças são aplicadas ao vivo sem reinicialização.

---

## Instalação e Execução Local

### Pré-requisitos
- Python 3.10+
- Conta na Binance com trading de Futuros habilitado
- Chave de API com **permissões de trading de Futuros** ativadas

### Passos

**1. Instalar dependências:**
```bash
pip install flask flask-socketio python-binance
```

**2. Configurar suas chaves de API:**

Edite o `config.json` com sua chave e segredo de API. Comece com `"is_demo": true` para usar o Testnet da Binance sem arriscar fundos reais.

**3. Iniciar o servidor:**
```bash
python app.py
```

**4. Abrir o navegador:**
```
http://localhost:3000
```

**5. Configure sua estratégia** no painel e clique em **Iniciar** quando estiver pronto.

---

## ☁️ Deploy no Railway (Recomendado)

O [Railway](https://railway.app) é a forma mais fácil de rodar este bot 24/7 na nuvem sem precisar manter seu computador ligado. Após o deploy, você recebe uma URL pública para acessar o painel de qualquer lugar.

### Passo 1 — Prepare seu Repositório

1. Crie uma conta gratuita no [github.com](https://github.com).
2. Crie um **novo repositório privado** (mantenha privado para proteger suas chaves de API!).
3. Compacte toda a pasta do projeto `NIGHT/` em um arquivo `.zip`.
4. Faça upload do zip ou envie os arquivos para seu repositório GitHub.

> 💡 Certifique-se de que `config.json` está incluído com suas chaves de API já preenchidas, **ou** use variáveis de ambiente (veja abaixo).

### Passo 2 — Deploy no Railway

1. Acesse [railway.app](https://railway.app) e entre com sua conta GitHub.
2. Clique em **"New Project"** → **"Deploy from GitHub repo"**.
3. Selecione seu repositório.
4. O Railway detectará automaticamente que é um projeto Python.

### Passo 3 — Configure o Comando de Início

Nas configurações do projeto no Railway, defina o **Start Command** como:
```
python app.py
```

Certifique-se de ter um arquivo `requirements.txt` na raiz do projeto:
```
flask
flask-socketio
python-binance
gunicorn
eventlet
```

### Passo 4 — Configure Variáveis de Ambiente (Opcional)

Em Railway → seu projeto → aba **Variables**, adicione:
- `PORT=3000`

O Railway atribuirá automaticamente uma URL pública como:
```
https://seu-projeto.up.railway.app
```

### Passo 5 — Acesse seu Painel

Clique na URL gerada no Railway. Seu painel estará ao vivo nessa URL. Você pode configurar e iniciar o bot de qualquer lugar do mundo.

> 🔒 **Dica de segurança:** Como o painel não tem autenticação por padrão, mantenha sua URL do Railway privada ou adicione uma camada de proteção acima dela.

---

## Arquitetura do Sistema

```
NIGHT/
├── app.py              # Servidor Flask, handlers Socket.IO, rotas REST da API
├── bot_engine.py       # Motor de trading principal, threads, comunicação com API da Binance
├── config.json         # Configuração ao vivo (editada pelo painel em tempo real)
├── translations_py.py  # Traduções das mensagens de log do backend (pt-BR / en-US)
├── binance_bot.log     # Arquivo de log gerado automaticamente
├── requirements.txt    # Dependências Python
├── templates/
│   └── dashboard.html  # Interface do painel (estrutura HTML, página única)
└── static/
    ├── css/style.css   # Tema escuro + todos os estilos personalizados
    └── js/app.js       # Toda a lógica frontend, listeners Socket.IO, atualizações de UI
```

### Principais Threads
| Thread | Propósito |
|---|---|
| Thread principal Flask/SocketIO | Serve HTTP + WebSocket para o navegador |
| `_global_background_worker` | Busca preços, alavancagem, info da exchange a cada ~1s |
| `ThreadedWebsocketManager` (por conta) | Escuta preenchimentos de ordens e atualizações de conta em tempo real |
| `_symbol_logic_worker` (por conta × símbolo) | Gerencia estado da grade e lógica de re-entrada por símbolo |

### Eventos Socket.IO Principais

| Evento | Direção | Descrição |
|---|---|---|
| `price_update` | Servidor → Navegador | Mapa de preços ao vivo para todos os símbolos |
| `max_leverages` | Servidor → Navegador | Alavancagem máxima da exchange por símbolo |
| `account_update` | Servidor → Navegador | Saldo, patrimônio, PnL, posições abertas (incluindo manuais) |
| `console_log` | Servidor → Navegador | Entradas de log traduzidas em tempo real |
| `bot_status` | Servidor → Navegador | Se o bot está em execução |
| `start_bot` | Navegador → Servidor | Usuário clicou em Iniciar |
| `stop_bot` | Navegador → Servidor | Usuário clicou em Parar |
| `close_trade` | Navegador → Servidor | Usuário clicou em Fechar em uma posição |

### Endpoints REST da API
| Endpoint | Método | Descrição |
|---|---|---|
| `/` | GET | Serve o HTML do painel |
| `/api/config` | GET | Retorna o `config.json` atual |
| `/api/config` | POST | Salva configuração atualizada e aplica ao vivo se o bot estiver rodando |
| `/api/close_position` | POST | Fecha uma posição específica a mercado |
| `/api/test_connection` | POST | Testa a conectividade das chaves de API |

---

## Notas Importantes

- **A alavancagem é automaticamente limitada** ao máximo permitido pela Binance para cada símbolo. Não é possível configurar 100x em um símbolo com máximo de 75x.
- **A precisão das ordens é tratada automaticamente.** Quantidade e preço são formatados exatamente conforme os requisitos de `stepSize` e `tickSize` da exchange.
- **Todas as configurações são por símbolo.** LINKUSDC e BTCUSDC podem ter estratégias, alavancagens e desvios completamente diferentes rodando simultaneamente.
- **O bot continua trabalhando nas ordens mesmo se o painel for fechado.** As ordens estão no livro da Binance — o bot apenas escuta notificações de preenchimento via WebSocket.
- **Comece com o Testnet.** Defina `"is_demo": true` no config.json para operar com segurança no Testnet da Binance Futures antes de ir para o mercado real.
