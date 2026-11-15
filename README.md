# Smart Trader Pro

Scanner de points d'entrée pour les cryptomonnaies. Il surveille les paires
USDT les plus liquides de Binance Spot, note chaque configuration sur 100 et
envoie une alerte quand plusieurs familles d'indicateurs s'accordent.

**Il analyse, il ne trade pas.** Aucune clé d'échange n'est demandée, aucun
ordre n'est passé. La décision reste la tienne.

Auteur : Aziz Mernissi

## Installation sur le VPS (copier-coller)

Connecte-toi en SSH au serveur, puis colle cette ligne :

```bash
curl -fsSL https://raw.githubusercontent.com/abouali50/smart-trader-pro/main/deploy/install.sh | sudo bash
```

Le script installe Python, télécharge le code dans `/opt/smart-trader-pro`, crée
un service qui redémarre tout seul, et lance la surveillance. Relancer la même
ligne met à jour le scanner sans toucher à tes réglages.

Voir ce qu'il fait :

```bash
journalctl -u smart-trader-pro -f
```

## Recevoir les signaux sur Telegram

1. Dans Telegram, écris à `@BotFather`, envoie `/newbot` et garde le jeton.
2. Écris un message à ton nouveau bot, puis ouvre
   `https://api.telegram.org/bot<JETON>/getUpdates` pour lire ton `chat id`.
3. Sur le VPS :

```bash
sudo nano /opt/smart-trader-pro/.env
sudo systemctl restart smart-trader-pro
cd /opt/smart-trader-pro && sudo -u smarttrader bash -c 'set -a; . ./.env; .venv/bin/python -m smart_trader_pro test-telegram'
```

## CoinGecko et CoinMarketCap

Ces deux sources donnent le contexte du marché ; elles ne déclenchent pas de
signal à elles seules.

| Source | Clé | Apporte |
| --- | --- | --- |
| CoinGecko | facultative (`COINGECKO_API_KEY`) | variation du marché global sur 24 h, dominance du BTC, cryptos en tendance |
| CoinMarketCap | obligatoire (`CMC_API_KEY`, gratuite sur pro.coinmarketcap.com) | dominance du BTC, indice Peur et Avidité |

Si le marché global perd plus de `market_drop_block_pct` % en 24 h, les
signaux sont suspendus. Une source en panne est ignorée, le scanner continue.

## Comment le score est calculé

| Famille | Points | Indicateurs |
| --- | --- | --- |
| Tendance | 30 | tendance 4 h (EMA50 et sa pente), EMA200, ADX avec +DI/-DI |
| Structure | 25 | repli sur l'EMA21, zone Fibonacci 0.382-0.618, médiane de Bollinger |
| Momentum | 20 | RSI, MACD, RSI stochastique |
| Volume | 20 | volume relatif, OBV, Chaikin Money Flow, MFI |
| Risque | 5 | prix pas trop éloigné de l'EMA21 (en ATR) |

Un signal exige une tendance de fond haussière **et** un score d'au moins
`min_score` (70 par défaut). Les familles sont plafonnées exprès : dix
indicateurs de momentum qui disent la même chose ne valent pas dix
confirmations.

Deux garde-fous s'ajoutent : pas de signal si la tendance de fond du BTC est
baissière, et pas deux alertes sur la même paire en moins de `cooldown_hours`.

Chaque alerte donne l'entrée, le stop (sous le dernier creux, entre 1 et 3
ATR), deux objectifs (1,5 R et 3 R), les niveaux de Fibonacci et la taille de
position pour ne risquer que `risk_pct` % du capital.

## Vérifier avant de faire confiance : le backtest

```bash
cd /opt/smart-trader-pro
sudo -u smarttrader .venv/bin/python -m smart_trader_pro backtest --symbols BTCUSDT ETHUSDT SOLUSDT --candles 5000
```

Hypothèses prudentes : entrée à l'ouverture de la bougie suivante, stop
prioritaire si stop et objectif sont touchés dans la même bougie, frais de
0,1 % par ordre. Regarde surtout `R moyen` (au-dessus de 0 = stratégie
gagnante sur la période) et compare à la colonne `Garder`.

## Réglages (`config.json`)

| Clé | Défaut | Rôle |
| --- | --- | --- |
| `symbols` | `"auto"` | `"auto"` = les plus liquides, ou une liste `["BTCUSDT", "ETHUSDT"]` |
| `auto_count` | 20 | nombre de paires en mode auto |
| `interval` | `"1h"` | unité de temps des signaux (la tendance est lue 4 fois plus haut) |
| `capital`, `risk_pct` | 1000, 1 | servent au calcul de la taille de position |
| `btc_filter` | `true` | suspend les signaux si le BTC est baissier |
| `market_drop_block_pct` | 5 | suspend les signaux si le marché chute de ce pourcentage en 24 h |
| `cooldown_hours` | 12 | délai minimal entre deux alertes sur une paire |
| `strategy.min_score` | 70 | plus haut = moins de signaux, plus sélectifs |

## Utilisation manuelle

```bash
python -m smart_trader_pro scan        # une passe
python -m smart_trader_pro watch       # surveillance continue
python -m smart_trader_pro backtest    # test sur l'historique
python -m pytest                  # tests du code
```

## Limites à connaître

- Aucun indicateur ne devine l'avenir. Le scanner repère des configurations
  favorables ; une partie finira au stop, c'est prévu dans le calcul.
- Un bon backtest ne garantit pas la suite, surtout si l'on règle les
  paramètres jusqu'à obtenir un beau résultat.
- Ne risque que de l'argent que tu peux perdre. Ceci n'est pas un conseil
  financier.

© 2026 Aziz Mernissi. Tous droits réservés.
