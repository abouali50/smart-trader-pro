# Smart Trader Pro

Scanner de cryptomonnaies sur Binance Spot, avec deux moteurs :

- **Radar d'explosion** : balaie environ 150 paires USDT et repère celles qui
  démarrent, c'est-à-dire qui cassent un plus haut avec un volume anormal et
  des acheteurs agressifs.
- **Replis en tendance** : sur les paires les plus liquides, attend un repli
  propre dans une tendance haussière.

Chaque configuration est notée sur 100 ; une alerte part quand plusieurs
familles d'indicateurs s'accordent.

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

## Tableau de bord web

Après l'installation, ouvre dans ton navigateur (ordinateur ou téléphone) :

```
http://ADRESSE_DU_SERVEUR:8765
```

La page montre le contexte du marché, les paires surveillées avec leur score
et les derniers signaux. Touche une ligne pour voir le détail des conditions.
Elle se met à jour toute seule chaque minute.

La page est en lecture seule : elle n'accepte aucune saisie et ne sert que
le tableau. Elle est toutefois visible par quiconque connaît l'adresse.
Pour changer de port, renseigne `DASHBOARD_PORT` dans `.env`.

### Avec un nom de domaine et HTTPS

Si le serveur utilise déjà Caddy, crée d'abord un enregistrement DNS de type
`A` pointant vers l'adresse du serveur, puis lance :

```bash
curl -fsSL https://raw.githubusercontent.com/abouali50/smart-trader-pro/main/deploy/domain.sh | sudo bash -s -- trader.exemple.com
```

Le script ajoute un bloc à la fin du Caddyfile après en avoir fait une
sauvegarde, et restaure celle-ci si Caddy refuse la configuration. Une fois
le site en ligne en HTTPS, l'accès direct par le port est fermé.

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

## Le radar d'explosion

Un mouvement violent commence presque toujours par une cassure accompagnée
d'un volume très supérieur à l'habitude. Le radar cherche ce moment.

| Famille | Points | Ce qui est mesuré |
| --- | --- | --- |
| Volume | 30 | volume de la bougie contre sa moyenne, volume des 4 dernières bougies, OBV au plus haut |
| Acheteurs | 10 | part du volume achetée au prix du marché (ordres agressifs) |
| Cassure | 30 | clôture au-dessus du plus haut des 48 dernières bougies, sortie de compression de Bollinger |
| Momentum | 15 | RSI, MACD, ADX, clôture près du plus haut |
| Force relative | 10 | performance face au Bitcoin sur 24 bougies |
| Risque | 5 | le départ est encore récent (moins de 15 % déjà parcourus) |

Un départ exige une cassure **et** un score d'au moins `radar_strategy.min_score`
(70 par défaut). Le radar ne se bloque pas quand le marché baisse : une crypto
peut partir seule. Il ne prédit pas l'avenir ; beaucoup de cassures échouent,
d'où le stop fourni avec chaque alerte.

Tester et comparer le radar sur l'historique :

```bash
sudo -u smarttrader .venv/bin/python -m smart_trader_pro backtest --mode radar
sudo -u smarttrader .venv/bin/python -m smart_trader_pro compare --mode radar
```

## Comment le score des replis est calculé

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

## Comparer des variantes sans se mentir

```bash
cd /opt/smart-trader-pro
sudo -u smarttrader .venv/bin/python -m smart_trader_pro compare
```

La commande coupe l'historique en deux. Elle mesure 24 variantes (tendance
lue en 4 h ou à la journée, score minimal, largeur du stop, objectif) sur la
première partie, puis juge la meilleure sur la seconde, qu'elle n'a jamais
vue. Seul ce second résultat compte. Le verdict dit si la variante tient
(au moins 30 trades, R moyen ≥ +0,10, PF ≥ 1,15) ou non.

Régler les paramètres jusqu'à obtenir un beau backtest ne prouve rien ; une
stratégie qui tient sur des données jamais vues est un indice sérieux, pas
une garantie.

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
| `radar` | `true` | active le radar d'explosion |
| `radar_count` | 150 | nombre de paires balayées par le radar |
| `radar_min_volume` | 3000000 | volume minimal sur 24 h, en USDT, pour qu'une paire soit balayée |
| `radar_cooldown_hours` | 24 | délai minimal entre deux alertes radar sur une paire |
| `radar_strategy.min_score` | 70 | seuil du radar |

## Utilisation manuelle

```bash
python -m smart_trader_pro scan        # une passe
python -m smart_trader_pro watch       # surveillance continue
python -m smart_trader_pro backtest    # test sur l'historique
python -m smart_trader_pro compare     # variantes, validées sur données jamais vues
python -m smart_trader_pro web         # tableau de bord web
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
